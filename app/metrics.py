"""结构化请求记录、指标汇总与告警判定。

设计要点：
- 每个端点保留一个时长为 ``window_seconds`` 的滚动窗口样本（双端队列，
  读取时淘汰过期样本），窗口数据量有上界，不会无限制增长；
- 另有按 状态码/错误类型/耗时区间 的固定维度累计计数；
- 最近请求明细使用有容量上界的环形缓冲；
- 告警采用**边沿触发**：阈值越界的当次请求产生且只产生一条事件，
  条件恢复后才允许再次触发，避免重复刷屏。
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass

from .config import AlertSettings
from .errors import ErrorType
from .ring_buffer import RingBuffer

#: 告警事件保留条数上界
_ALERT_CAPACITY = 100

#: 视为"服务错误"的状态码判定：仅 5xx 计入错误率
def _is_server_error(status_code: int) -> bool:
    return status_code >= 500


def latency_bucket_label(duration_ms: float, boundaries: tuple[int, ...]) -> str:
    """把耗时归入左闭右开区间，最后一档为溢出档。"""
    prev = 0
    for edge in boundaries:
        if duration_ms < edge:
            return f"{prev}-{edge}ms"
        prev = edge
    return f"{boundaries[-1]}ms+"


def quantile(sorted_values: list[float], q: float) -> float:
    """nearest-rank 分位数；空样本返回 0.0。"""
    if not sorted_values:
        return 0.0
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    rank = math.ceil(q * len(sorted_values))
    return sorted_values[rank - 1]


@dataclass(frozen=True)
class RequestRecord:
    request_id: str
    endpoint: str
    method: str
    status_code: int
    duration_ms: float
    error_type: ErrorType | None
    timestamp: float
    #: 是否为入口即被拒绝的请求（非法标识等），走独立有界缓冲
    rejected: bool = False
    #: 拒绝原因（仅被拒请求使用，承载安全可解释文本）
    reject_reason: str | None = None
    #: 下游响应回传的标识（跨服务链路关联）
    downstream_request_id: str | None = None


@dataclass(frozen=True)
class EndpointMetrics:
    endpoint: str
    window_seconds: float
    total: int
    cumulative_total: int
    by_status: dict[int, int]
    by_error_type: dict[str, int]
    by_latency_bucket: dict[str, int]
    error_rate: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float


@dataclass(frozen=True)
class AlertEvent:
    alert_id: str
    endpoint: str
    kind: str  # "high_error_rate" | "high_latency"
    observed_value: float
    threshold: float
    window_seconds: float
    sample_size: int
    request_id: str
    timestamp: float
    message: str


@dataclass(frozen=True)
class _Sample:
    timestamp: float
    status_code: int
    duration_ms: float
    error_type: ErrorType | None


class MetricsRegistry:
    """按端点汇总指标、保留近期明细、在阈值越界时产生告警。"""

    def __init__(
        self,
        alert_settings: AlertSettings,
        recent_capacity: int,
        latency_buckets_ms: tuple[int, ...],
        rejected_capacity: int | None = None,
    ) -> None:
        self._alert_settings = alert_settings
        self._buckets = tuple(latency_buckets_ms)
        self._recent: RingBuffer[RequestRecord] = RingBuffer(recent_capacity)
        # 被拒请求独立缓冲：与正常明细容量各自硬上界，互不挤占
        self._rejected: RingBuffer[RequestRecord] = RingBuffer(
            rejected_capacity if rejected_capacity is not None else recent_capacity
        )
        self._alerts: RingBuffer[AlertEvent] = RingBuffer(_ALERT_CAPACITY)
        # 端点 -> 窗口样本
        self._windows: dict[str, deque[_Sample]] = {}
        # 端点 -> 累计总量（其余累计维度随窗口快照按需计算，固定键集合）
        self._cumulative: dict[str, int] = {}
        # (端点, kind) -> 是否处于告警态（用于边沿触发）
        self._firing: dict[tuple[str, str], bool] = {}
        self._alert_seq = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    def record(self, rec: RequestRecord, now: float | None = None) -> list[AlertEvent]:
        """记录一次请求，返回本次新产生的告警事件（通常 0~2 条）。"""
        current = time.time() if now is None else now
        with self._lock:
            if rec.rejected:
                self._rejected.add(rec)
            else:
                self._recent.add(rec)
            self._cumulative[rec.endpoint] = self._cumulative.get(rec.endpoint, 0) + 1
            window = self._windows.setdefault(rec.endpoint, deque())
            self._prune(rec.endpoint, current)
            # 上一个统计窗口已完全滚动过去：告警态视为已恢复，允许再次触发
            if not window:
                self._firing[(rec.endpoint, "high_error_rate")] = False
                self._firing[(rec.endpoint, "high_latency")] = False
            window.append(
                _Sample(rec.timestamp, rec.status_code, rec.duration_ms, rec.error_type)
            )
            return self._evaluate_alerts(rec.endpoint, current, rec.request_id, rec.timestamp)

    def _prune(self, endpoint: str, now: float) -> None:
        window = self._windows[endpoint]
        horizon = now - self._alert_settings.window_seconds
        while window and window[0].timestamp < horizon:
            window.popleft()

    # ------------------------------------------------------------------ #
    def _evaluate_alerts(
        self, endpoint: str, now: float, request_id: str, event_time: float
    ) -> list[AlertEvent]:
        cfg = self._alert_settings
        window = self._windows[endpoint]
        n = len(window)
        events: list[AlertEvent] = []
        if n < cfg.min_requests:
            # 样本不足，复位告警态，不产生事件
            self._firing[(endpoint, "high_error_rate")] = False
            self._firing[(endpoint, "high_latency")] = False
            return events

        errors = sum(1 for s in window if _is_server_error(s.status_code))
        error_rate = errors / n
        latencies = sorted(s.duration_ms for s in window)
        p_q = quantile(latencies, cfg.latency_quantile)

        firing_err = self._firing.get((endpoint, "high_error_rate"), False)
        if error_rate >= cfg.error_rate_threshold:
            if not firing_err:
                events.append(
                    self._make_alert(
                        endpoint,
                        "high_error_rate",
                        observed=error_rate,
                        threshold=cfg.error_rate_threshold,
                        sample_size=n,
                        request_id=request_id,
                        event_time=event_time,
                        message=(
                            f"端点 {endpoint} 最近 {cfg.window_seconds:g}s 错误率 "
                            f"{error_rate:.0%}（{errors}/{n}）达到阈值 "
                            f"{cfg.error_rate_threshold:.0%}"
                        ),
                    )
                )
                self._firing[(endpoint, "high_error_rate")] = True
        else:
            self._firing[(endpoint, "high_error_rate")] = False

        firing_lat = self._firing.get((endpoint, "high_latency"), False)
        if p_q >= cfg.latency_threshold_ms:
            if not firing_lat:
                events.append(
                    self._make_alert(
                        endpoint,
                        "high_latency",
                        observed=p_q,
                        threshold=cfg.latency_threshold_ms,
                        sample_size=n,
                        request_id=request_id,
                        event_time=event_time,
                        message=(
                            f"端点 {endpoint} 最近 {cfg.window_seconds:g}s "
                            f"p{cfg.latency_quantile * 100:g} 延迟 {p_q:.1f}ms "
                            f"达到阈值 {cfg.latency_threshold_ms:.1f}ms（样本 {n}）"
                        ),
                    )
                )
                self._firing[(endpoint, "high_latency")] = True
        else:
            self._firing[(endpoint, "high_latency")] = False

        return events

    def _make_alert(
        self,
        endpoint: str,
        kind: str,
        observed: float,
        threshold: float,
        sample_size: int,
        request_id: str,
        event_time: float,
        message: str,
    ) -> AlertEvent:
        self._alert_seq += 1
        event = AlertEvent(
            alert_id=f"alert-{self._alert_seq:06d}",
            endpoint=endpoint,
            kind=kind,
            observed_value=round(observed, 6),
            threshold=threshold,
            window_seconds=self._alert_settings.window_seconds,
            sample_size=sample_size,
            request_id=request_id,
            timestamp=event_time,
            message=message,
        )
        self._alerts.add(event)
        return event

    # ------------------------------------------------------------------ #
    def _snapshot_metrics(self, endpoint: str, now: float) -> EndpointMetrics | None:
        self._prune(endpoint, now)
        window = self._windows.get(endpoint)
        if not window:
            return None
        by_status: dict[int, int] = {}
        by_error_type: dict[str, int] = {}
        by_bucket: dict[str, int] = {}
        durations: list[float] = []
        for s in window:
            by_status[s.status_code] = by_status.get(s.status_code, 0) + 1
            if s.error_type is not None:
                key = s.error_type.value
                by_error_type[key] = by_error_type.get(key, 0) + 1
            label = latency_bucket_label(s.duration_ms, self._buckets)
            by_bucket[label] = by_bucket.get(label, 0) + 1
            durations.append(s.duration_ms)
        durations.sort()
        errors = sum(1 for s in window if _is_server_error(s.status_code))
        return EndpointMetrics(
            endpoint=endpoint,
            window_seconds=self._alert_settings.window_seconds,
            total=len(window),
            cumulative_total=self._cumulative.get(endpoint, 0),
            by_status=dict(sorted(by_status.items())),
            by_error_type=dict(sorted(by_error_type.items())),
            by_latency_bucket=by_bucket,
            error_rate=errors / len(window),
            latency_p50_ms=quantile(durations, 0.50),
            latency_p95_ms=quantile(durations, 0.95),
            latency_p99_ms=quantile(durations, 0.99),
        )

    def metrics(self, endpoint: str, now: float | None = None) -> EndpointMetrics | None:
        current = time.time() if now is None else now
        with self._lock:
            return self._snapshot_metrics(endpoint, current)

    def all_metrics(self, now: float | None = None) -> list[EndpointMetrics]:
        current = time.time() if now is None else now
        with self._lock:
            out = []
            for endpoint in list(self._windows.keys()):
                m = self._snapshot_metrics(endpoint, current)
                if m is not None:
                    out.append(m)
            return sorted(out, key=lambda m: m.endpoint)

    def recent(
        self,
        limit: int | None = None,
        *,
        request_id: str | None = None,
        include_rejected: bool = True,
    ) -> list[RequestRecord]:
        """近期明细，新 -> 旧；limit 截断。

        合并正常明细与被拒明细（各自受环形缓冲容量硬上界约束），
        按时间戳倒序；``request_id`` 非空时只返回该标识对应的记录，
        用于"拿标识查回溯"。
        """
        with self._lock:
            items = self._recent.snapshot()
            if include_rejected:
                items = items + self._rejected.snapshot()
        if request_id is not None:
            items = [r for r in items if r.request_id == request_id]
        items.sort(key=lambda r: r.timestamp, reverse=True)
        return items if limit is None else items[:limit]

    def recent_capacity(self) -> int:
        return self._recent.capacity

    def rejected_capacity(self) -> int:
        return self._rejected.capacity

    def alerts(self) -> list[AlertEvent]:
        return self._alerts.snapshot()

    def reset(self) -> None:
        """测试/运维复位。"""
        with self._lock:
            self._recent.clear()
            self._rejected.clear()
            self._alerts.clear()
            self._windows.clear()
            self._cumulative.clear()
            self._firing.clear()
            self._alert_seq = 0
