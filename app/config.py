"""配置：标识规则、指标窗口、告警阈值。

所有阈值均在此集中定义，便于文档与测试引用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class RequestIdSettings:
    #: 接收/回传请求标识的 HTTP 头
    header_name: str = "X-Request-ID"
    #: 服务端生成标识的固定前缀
    prefix: str = "req-"
    #: 调用方提供标识的合法字符规则（首字符为字母数字，其余可为 . _ -）
    valid_pattern: str = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    #: 调用方提供标识的最大长度
    max_length: int = 128


@dataclass(frozen=True)
class AlertSettings:
    #: 判定窗口（秒）：只统计最近该时长内的请求
    window_seconds: float = 60.0
    #: 窗口内样本数下限：少于该值不产生告警（避免小样本噪声）
    min_requests: int = 5
    #: 错误率阈值：窗口内 5xx/归类错误占比 >= 该值即告警
    error_rate_threshold: float = 0.5
    #: 延迟分位（0.95 表示 p95）
    latency_quantile: float = 0.95
    #: 延迟阈值（毫秒）：指定分位 >= 该值即告警
    latency_threshold_ms: float = 500.0


@dataclass(frozen=True)
class MetricsSettings:
    #: 近期请求明细的保留条数上界（环形缓冲容量）
    recent_records_capacity: int = 200
    #: 耗时区间边界（毫秒，左闭右开，最后一档为溢出档）
    latency_buckets_ms: tuple[int, ...] = (50, 100, 200, 500, 1000)


@dataclass(frozen=True)
class Settings:
    request_id: RequestIdSettings = RequestIdSettings()
    alert: AlertSettings = AlertSettings()
    metrics: MetricsSettings = MetricsSettings()


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def get_settings() -> Settings:
    """读取默认配置；告警阈值支持通过环境变量覆盖。

    环境变量：
      OBS_ALERT_WINDOW_SECONDS / OBS_ALERT_MIN_REQUESTS /
      OBS_ALERT_ERROR_RATE_THRESHOLD / OBS_ALERT_LATENCY_THRESHOLD_MS /
      OBS_METRICS_RECENT_CAPACITY
    """
    alert = AlertSettings(
        window_seconds=_env_float("OBS_ALERT_WINDOW_SECONDS", AlertSettings.window_seconds),
        min_requests=_env_int("OBS_ALERT_MIN_REQUESTS", AlertSettings.min_requests),
        error_rate_threshold=_env_float(
            "OBS_ALERT_ERROR_RATE_THRESHOLD", AlertSettings.error_rate_threshold
        ),
        latency_threshold_ms=_env_float(
            "OBS_ALERT_LATENCY_THRESHOLD_MS", AlertSettings.latency_threshold_ms
        ),
    )
    metrics = MetricsSettings(
        recent_records_capacity=_env_int(
            "OBS_METRICS_RECENT_CAPACITY", MetricsSettings.recent_records_capacity
        ),
    )
    return Settings(alert=replace(alert), metrics=metrics)
