"""结构化请求记录、指标汇总、有界回溯明细与告警判定。

设计要点
--------
* 每个请求结束后生成一条 :class:`RequestRecord` 送入 :class:`MetricsRegistry`；
* 指标按 *端点* 聚合：总量、按状态码、按耗时区间、按错误类型计数；
* 近期明细保存在一个**全局有上界**的 ``deque`` 中（容量由构造参数决定，
  默认 1000 条），最旧记录被自动淘汰，长期运行内存占用恒定；
* 告警判定基于端点最近 ``window_size`` 条记录的滑动窗口，分别评估
  服务端错误率与慢请求率；同一端点同一类告警在冷却期内不重复产生。

线程/并发安全：FastAPI 可能在线程池中执行同步端点，因此所有状态变更
都在同一把 ``RLock`` 内完成（计算量很小，锁粒度足够）。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum

from .errors import ErrorCategory


class LatencyBucket(str, Enum):
    """耗时区间（左闭右开，最后一档为闭区间上界）。"""

    LE_50MS = "0-50ms"
    LE_100MS = "50-100ms"
    LE_200MS = "100-200ms"
    LE_500MS = "200-500ms"
    GT_500MS = "500ms+"

    @classmethod
    def for_latency(cls, latency_ms: float) -> "LatencyBucket":
        if latency_ms < 50:
            return cls.LE_50MS
        if latency_ms < 100:
            return cls.LE_100MS
        if latency_ms < 200:
            return cls.LE_200MS
        if latency_ms < 500:
            return cls.LE_500MS
        return cls.GT_500MS


@dataclass(frozen=True)
class Latency:
    pass


@dataclass(frozen=True)
class RequestRecord:
    """单次请求的结构化记录（不可变，可安全跨线程读取）。"""

    request_id: str
    endpoint: str
    status_code: int
    latency_ms: float
    error_category: ErrorCategory | None
    timestamp: float

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "endpoint": self.endpoint,
            "status_code": self.status_code,
            "latency_ms": round(self.latency_ms, 3),
            "error_category": self.error_category.value
            if self.error_category
            else None,
            "timestamp": self.timestamp,
        }


@dataclass
class AlertRule:
    """告警阈值语义。

    * ``min_samples``：窗口内至少有多少条样本才开始评估，避免冷启动抖动；
    * ``error_rate_threshold``：服务端错误（状态码 >= 500）占比超过该值
      触发错误率告警（取值 0~1，如 0.5 表示 >50%）；
    * ``slow_rate_threshold``：慢请求占比超过该值触发延迟告警；
    * ``slow_latency_ms``：延迟达到该值（含）记为一个慢请求。

    判定使用严格大于 ``>``：恰好等于阈值不触发，语义明确、可复现。
    """

    name: str
    min_samples: int
    error_rate_threshold: float
    slow_rate_threshold: float
    slow_latency_ms: float


@dataclass(frozen=True)
class AlertEvent:
    """一次可解释的告警事件。"""

    timestamp: float
    endpoint: str
    kind: str  # "error_rate" | "latency"
    observed: float
    threshold: float
    sample_size: int
    message: str
    request_id: str

    def as_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "endpoint": self.endpoint,
            "kind": self.kind,
            "observed": round(self.observed, 4),
            "threshold": self.threshold,
            "sample_size": self.sample_size,
            "message": self.message,
            "request_id": self.request_id,
        }


class _EndpointCounters:
    """单端点的增量计数器。"""

    __slots__ = (
        "total", "status_codes", "latency_buckets", "error_categories",
        "latency_sum_ms", "server_error_total",
    )

    def __init__(self) -> None:
        self.total = 0
        self.status_codes: dict[int, int] = {}
        self.latency_buckets: dict[str, int] = {}
        self.error_categories: dict[str, int] = {}
        self.latency_sum_ms = 0.0
        self.server_error_total = 0


#: 默认阈值（测试与文档中均引用同一事实来源）。
DEFAULT_RULE = AlertRule(
    name="default",
    min_samples=10,
    error_rate_threshold=0.5,
    slow_rate_threshold=0.5,
    slow_latency_ms=200.0,
)

#: 同一端点同一类告警的冷却时间（秒），防止持续超阈值时告警风暴。
ALERT_COOLDOWN_SECONDS = 60.0


class MetricsRegistry:
    """线程安全的指标注册表。"""

    def __init__(
        self,
        capacity: int = 1000,
        rule: AlertRule | None = None,
        window_size: int = 100,
        alert_history: int = 100,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._rule = rule or DEFAULT_RULE
        self._window_size = window_size
        self._alert_history_cap = alert_history
        self._lock = threading.RLock()
        # 全局明细：FIFO，容量即资源占用上界。
        self._records: deque[RequestRecord] = deque(maxlen=capacity)
        self._counters: dict[str, _EndpointCounters] = {}
        self._alerts: deque[AlertEvent] = deque(maxlen=alert_history)
        # 冷却：(endpoint, kind) -> 上次告警时间戳
        self._last_alert_at: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def record(self, rec: RequestRecord) -> list[AlertEvent]:
        """记录一次请求，返回本次新产生的告警事件（可能为空）。"""
        with self._lock:
            self._records.append(rec)
            c = self._counters.setdefault(rec.endpoint, _EndpointCounters())
            c.total += 1
            c.status_codes[rec.status_code] = c.status_codes.get(rec.status_code, 0) + 1
            bucket = LatencyBucket.for_latency(rec.latency_ms).value
            c.latency_buckets[bucket] = c.latency_buckets.get(bucket, 0) + 1
            c.latency_sum_ms += rec.latency_ms
            if rec.status_code >= 500:
                c.server_error_total += 1
            if rec.error_category is not None:
                key = rec.error_category.value
                c.error_categories[key] = c.error_categories.get(key, 0) + 1
            return self._evaluate(rec)

    def _evaluate(self, latest: RequestRecord) -> list[AlertEvent]:
        """在锁内评估最近窗口的错误率/慢请求率是否超阈。"""
        rule = self._rule
        # 取该端点最近 window_size 条（明细是全局 FIFO，反向扫描即可）。
        window: deque[RequestRecord] = deque(maxlen=self._window_size)
        for rec in reversed(self._records):
            if rec.endpoint == latest.endpoint:
                window.appendleft(rec)
                if len(window) >= self._window_size:
                    break
        n = len(window)
        if n < rule.min_samples:
            return []

        errors = sum(1 for r in window if r.status_code >= 500)
        slow = sum(1 for r in window if r.latency_ms >= rule.slow_latency_ms)
        error_rate = errors / n
        slow_rate = slow / n
        now = latest.timestamp

        events: list[AlertEvent] = []
        if error_rate > rule.error_rate_threshold:
            event = self._maybe_alert(
                now, latest.endpoint, "error_rate", error_rate,
                rule.error_rate_threshold, n, latest.request_id,
                f"error rate {error_rate:.0%} over last {n} requests "
                f"exceeds threshold {rule.error_rate_threshold:.0%}",
            )
            if event is not None:
                events.append(event)
        if slow_rate > rule.slow_rate_threshold:
            event = self._maybe_alert(
                now, latest.endpoint, "latency", slow_rate,
                rule.slow_rate_threshold, n, latest.request_id,
                f"slow-request rate {slow_rate:.0%} "
                f"(latency >= {rule.slow_latency_ms:g}ms) over last {n} "
                f"requests exceeds threshold {rule.slow_rate_threshold:.0%}",
            )
            if event is not None:
                events.append(event)
        return events

    def _maybe_alert(
        self, now: float, endpoint: str, kind: str, observed: float,
        threshold: float, n: int, request_id: str, message: str,
    ) -> AlertEvent | None:
        last = self._last_alert_at.get((endpoint, kind))
        if last is not None and (now - last) < ALERT_COOLDOWN_SECONDS:
            return None
        event = AlertEvent(
            timestamp=now, endpoint=endpoint, kind=kind, observed=observed,
            threshold=threshold, sample_size=n, message=message,
            request_id=request_id,
        )
        self._alerts.append(event)
        self._last_alert_at[(endpoint, kind)] = now
        return event

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def stats(self, endpoint: str) -> dict | None:
        with self._lock:
            c = self._counters.get(endpoint)
            if c is None:
                return None
            return self._stats_snapshot(endpoint, c)

    def _stats_snapshot(self, endpoint: str, c: _EndpointCounters) -> dict:
        avg = c.latency_sum_ms / c.total if c.total else 0.0
        return {
            "endpoint": endpoint,
            "total": c.total,
            "by_status_code": {str(k): v for k, v in sorted(c.status_codes.items())},
            "by_latency_bucket": {
                b.value: c.latency_buckets.get(b.value, 0) for b in LatencyBucket
            },
            "by_error_category": dict(sorted(c.error_categories.items())),
            "server_error_total": c.server_error_total,
            "average_latency_ms": round(avg, 3),
        }

    def all_stats(self) -> dict:
        with self._lock:
            return {
                ep: self._stats_snapshot(ep, c)
                for ep, c in sorted(self._counters.items())
            }

    def recent(
        self, endpoint: str | None = None, limit: int = 20
    ) -> list[dict]:
        with self._lock:
            items = list(self._records)
        if endpoint is not None:
            items = [r for r in items if r.endpoint == endpoint]
        if limit < len(items):
            items = items[-limit:]
        return [r.as_dict() for r in items]

    def alerts(self, limit: int = 20) -> list[dict]:
        with self._lock:
            items = list(self._alerts)
        return [e.as_dict() for e in items[-limit:]]

    @property
    def capacity(self) -> int:
        return self._capacity

    def reset(self) -> None:
        """清空全部状态（测试与运维重置使用）。"""
        with self._lock:
            self._records.clear()
            self._counters.clear()
            self._alerts.clear()
            self._last_alert_at.clear()


#: 进程级默认注册表（中间件与查询接口共用）。
registry = MetricsRegistry()
