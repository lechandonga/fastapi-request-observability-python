"""FastAPI 应用装配与路由。

除原有 ``GET /`` 外，额外提供：
- 演示路由：正常/业务异常/依赖异常/未捕获异常/超时/参数校验，供本地验证；
- ``GET /observability/metrics``：按端点的指标汇总；
- ``GET /observability/recent``：有上界的近期请求明细（新 -> 旧）；
- ``GET /observability/alerts``：告警事件列表。
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from fastapi import FastAPI, Query
from pydantic import BaseModel

from .config import Settings, get_settings
from .errors import BusinessError, DependencyError
from .exception_handlers import register_exception_handlers
from .logging_setup import configure_logging
from .metrics import AlertEvent, MetricsRegistry, RequestRecord
from .middleware import ObservabilityMiddleware
from .request_id import get_request_id


def _record_to_dict(rec: RequestRecord) -> dict:
    d = asdict(rec)
    d["error_type"] = rec.error_type.value if rec.error_type else None
    return d


def _alert_to_dict(event: AlertEvent) -> dict:
    return asdict(event)


def build_app(settings: Settings | None = None) -> FastAPI:
    """构造应用；settings 为 None 时读取默认配置。"""
    cfg = settings or get_settings()
    configure_logging()
    app = FastAPI(title="fastapi-request-observability", version="0.1.0")

    registry = MetricsRegistry(
        alert_settings=cfg.alert,
        recent_capacity=cfg.metrics.recent_records_capacity,
        latency_buckets_ms=cfg.metrics.latency_buckets_ms,
    )
    # 暴露给测试与查询路由
    app.state.registry = registry
    app.state.settings = cfg

    app.add_middleware(
        ObservabilityMiddleware,
        registry=registry,
        header_name=cfg.request_id.header_name,
    )
    register_exception_handlers(app, header_name=cfg.request_id.header_name)

    class EchoBody(BaseModel):
        message: str
        n: int = 1

    @app.get("/")
    def read_root() -> dict:
        # 对外行为保持与骨架完全兼容
        return {"Hello": "World"}

    @app.get("/echo")
    def echo(msg: str = Query(..., min_length=1, max_length=64)) -> dict:
        """正常路径：回显并携带当前请求标识（演示内部传播）。"""
        return {"request_id": get_request_id(), "msg": msg}

    @app.get("/items/{item_id}")
    def get_item(item_id: int) -> dict:
        """路径参数校验失败会走 422（非整数）。"""
        return {"request_id": get_request_id(), "item_id": item_id}

    @app.post("/echo")
    def echo_body(body: EchoBody) -> dict:
        return {"request_id": get_request_id(), "echo": body.message, "n": body.n}

    @app.get("/demo/business-error")
    def demo_business_error() -> dict:
        # 内部文本不对外，仅在诊断日志中可见
        raise BusinessError("内部账户 9527 余额计算失败的敏感细节")

    @app.get("/demo/dependency-error")
    def demo_dependency_error() -> dict:
        raise DependencyError("内部依赖 http://10.0.0.7:6379 连接被拒")

    @app.get("/demo/uncaught")
    def demo_uncaught() -> dict:
        raise RuntimeError("secret internal stack frame at db.query()")

    @app.get("/demo/timeout")
    async def demo_timeout() -> dict:
        raise asyncio.TimeoutError()

    @app.get("/demo/slow")
    async def demo_slow(ms: int = Query(600, ge=0, le=10000)) -> dict:
        await asyncio.sleep(ms / 1000.0)
        return {"request_id": get_request_id(), "slept_ms": ms}

    # ------------------------------------------------------------------ #
    @app.get("/observability/metrics")
    def get_metrics(endpoint: str | None = None) -> dict:
        if endpoint is not None:
            m = registry.metrics(endpoint)
            return {
                "settings": _alert_settings_dict(cfg),
                "metrics": asdict(m) if m is not None else None,
            }
        return {
            "settings": _alert_settings_dict(cfg),
            "metrics": [asdict(m) for m in registry.all_metrics()],
        }

    @app.get("/observability/recent")
    def get_recent(limit: int = Query(20, ge=1, le=200)) -> dict:
        records = registry.recent(limit=limit)
        return {
            "capacity": cfg.metrics.recent_records_capacity,
            "returned": len(records),
            "records": [_record_to_dict(r) for r in records],
        }

    @app.get("/observability/alerts")
    def get_alerts() -> dict:
        events = registry.alerts()
        return {"count": len(events), "alerts": [_alert_to_dict(e) for e in events]}

    return app


def _alert_settings_dict(cfg: Settings) -> dict:
    return {
        "window_seconds": cfg.alert.window_seconds,
        "min_requests": cfg.alert.min_requests,
        "error_rate_threshold": cfg.alert.error_rate_threshold,
        "latency_quantile": cfg.alert.latency_quantile,
        "latency_threshold_ms": cfg.alert.latency_threshold_ms,
        "recent_records_capacity": cfg.metrics.recent_records_capacity,
    }
