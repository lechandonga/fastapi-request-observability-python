"""演示路由与可观测性查询接口。

对外行为兼容：``GET /`` 仍返回 ``{"Hello": "World"}``。
其余路由用于演示错误归类、标识传播与指标/告警回溯。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, Request

from .context import REQUEST_ID_HEADER, downstream_headers, get_request_id
from .errors import UpstreamError, UpstreamTimeoutError
from .metrics import DEFAULT_RULE, MetricsRegistry

router = APIRouter()


def _registry(request: Request) -> MetricsRegistry:
    """从 app.state 取注册表（测试可注入），回退到中间件绑定的实例。"""
    return request.app.state.metrics  # 由 main.py 在启动时保证存在


@router.get("/")
async def read_root() -> dict:
    """原有健康/示例接口，响应体保持不变。"""
    return {"Hello": "World"}


@router.get("/echo/request-id")
async def echo_request_id(request: Request) -> dict:
    """回显当前请求标识及其来源（调用方提供 or 服务端生成）。"""
    rid = get_request_id()
    return {
        REQUEST_ID_HEADER.lower(): rid,
        "received": request.headers.get(REQUEST_ID_HEADER.lower()) == rid,
    }


@router.get("/error/upstream")
async def error_upstream() -> dict:
    """演示已归类的下游错误（502, upstream_error）。"""
    raise UpstreamError("simulated upstream failure")


@router.get("/error/timeout")
async def error_timeout() -> dict:
    """演示下游超时错误（504, timeout）。"""
    raise UpstreamTimeoutError("simulated upstream timeout")


@router.get("/error/unhandled")
async def error_unhandled() -> dict:
    """演示未捕获异常：由中间件兜底归类为 internal_error（500）。"""
    raise RuntimeError("simulated unexpected failure")


@router.get("/slow")
async def slow(ms: int = Query(default=0, ge=0, le=10_000)) -> dict:
    """按参数睡眠，便于稳定制造慢请求样本（受参数校验约束）。"""
    await asyncio.sleep(ms / 1000.0)
    return {"slept_ms": ms}


@router.get("/internal/downstream-call")
async def downstream_call() -> dict:
    """演示内部调用时的标识传播：返回会带给下游的请求头。"""
    return {"propagated_headers": downstream_headers(), "request_id": get_request_id()}


@router.get("/internal/isolation-probe")
async def isolation_probe() -> dict:
    """并发隔离探针：让出事件循环后再次读取标识，验证 ContextVar 不串扰。"""
    seen_before = get_request_id()
    await asyncio.sleep(0.01)  # 强制交错调度，放大并发下的串扰窗口
    seen_after = get_request_id()
    propagated = downstream_headers()
    return {
        "seen_before": seen_before,
        "seen_after": seen_after,
        "propagated": propagated,
        "consistent": seen_before == seen_after == propagated.get(REQUEST_ID_HEADER),
    }


@router.get("/internal/metrics")
async def get_metrics(request: Request) -> dict:
    """返回全部端点的汇总指标。"""
    return {
        "rule": {
            "min_samples": DEFAULT_RULE.min_samples,
            "error_rate_threshold": DEFAULT_RULE.error_rate_threshold,
            "slow_rate_threshold": DEFAULT_RULE.slow_rate_threshold,
            "slow_latency_ms": DEFAULT_RULE.slow_latency_ms,
        },
        "endpoints": _registry(request).all_stats(),
    }


@router.get("/internal/recent")
async def get_recent(
    request: Request,
    endpoint: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """返回近期请求明细（全局容量有上界，见 /internal/metrics 同级配置）。"""
    reg = _registry(request)
    items = reg.recent(endpoint=endpoint, limit=limit)
    return {
        "capacity": reg.capacity,
        "returned": len(items),
        "items": items,
    }


@router.get("/internal/alerts")
async def get_alerts(
    request: Request, limit: int = Query(default=20, ge=1, le=100)
) -> dict:
    """返回近期告警事件。"""
    items = _registry(request).alerts(limit=limit)
    return {"returned": len(items), "items": items}
