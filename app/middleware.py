"""可观测性 ASGI 中间件。

职责（每个 HTTP 请求）：

1. 读取 ``X-Request-ID``：缺失则生成、非法则直接拒绝（400）；
2. 把标识绑定到 ContextVar，请求结束后复位，保证并发隔离；
3. 用 ``time.monotonic`` 测量耗时，包装 ``send`` 捕获真实状态码；
4. 在所有响应（含错误响应）上回写 ``X-Request-ID`` 头；
5. 捕获漏过框架异常处理器的异常，归类后返回安全 JSON，堆栈只入日志；
6. 写入一条结构化请求记录，并把新告警以 warning 日志输出。
"""
from __future__ import annotations

import logging
import time

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .context import (
    REQUEST_ID_HEADER,
    generate_request_id,
    reset_request_id,
    resolve_request_id,
    set_request_id,
)
from .errors import (
    ErrorCategory,
    InvalidRequestIdError,
    classify_exception,
    error_body,
    public_message_for,
    status_for_category,
)
from .metrics import MetricsRegistry, RequestRecord

logger = logging.getLogger("app.request")

#: 未进入路由（非法标识在路由前拒绝）时使用的聚合端点名。
PRE_ROUTE_ENDPOINT = "-"
#: 未匹配到路由（如 404）时使用的聚合端点名，避免任意路径打散指标。
UNMATCHED_ENDPOINT = "unmatched"


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp, metrics: MetricsRegistry | None = None) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw = Headers(scope=scope).get(REQUEST_ID_HEADER.lower())
        try:
            request_id, generated = resolve_request_id(raw)
        except InvalidRequestIdError:
            # 拒绝路径同样需要诊断标识：生成一个 req- 标识贯穿日志与响应。
            diag_id = generate_request_id()
            token = set_request_id(diag_id)
            logger.warning(
                "rejected request with invalid request id",
                extra={"raw_request_id": raw, "reason": "validation_failed"},
            )
            response = JSONResponse(
                status_code=400,
                content=error_body(
                    ErrorCategory.INVALID_REQUEST_ID,
                    "invalid X-Request-ID header: use 8-128 chars of "
                    "letters/digits/_/- starting with a letter or digit",
                    diag_id,
                ),
            )
            try:
                await self._record(
                    scope, response, diag_id, PRE_ROUTE_ENDPOINT, 0.0,
                    ErrorCategory.INVALID_REQUEST_ID,
                )
                await response(scope, receive, self._wrapped_send(send, diag_id))
            finally:
                reset_request_id(token)
            return

        token = set_request_id(request_id)
        start = time.monotonic()
        status_holder: dict[str, int] = {"status": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
                # 注入/覆盖 X-Request-ID（去掉调用方同名头，杜绝重复与伪造）。
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.decode("latin1").lower() != REQUEST_ID_HEADER.lower()
                ]
                headers.append(
                    (
                        REQUEST_ID_HEADER.lower().encode("latin1"),
                        request_id.encode("latin1"),
                    )
                )
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:  # 框架处理器未覆盖的漏网异常
            category = classify_exception(exc)
            status = status_for_category(category)
            logger.error(
                "unhandled exception during request",
                extra={
                    "error_category": category.value,
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                },
                exc_info=True,
            )
            response = JSONResponse(
                status_code=status,
                content=error_body(
                    category, public_message_for(exc, category), request_id
                ),
            )
            elapsed_ms = (time.monotonic() - start) * 1000.0
            await self._record(
                scope, response, request_id, self._endpoint(scope),
                elapsed_ms, category,
            )
            await response(scope, receive, self._wrapped_send(send, request_id))
        else:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            self._log_completion(scope, request_id, generated, status_holder["status"], elapsed_ms)
            if self.metrics is not None:
                # 优先使用框架异常处理器经 scope 回传的错误类型；
                # 未被处理器分类的 5xx 兜底为 internal_error。
                category = scope.get("obs_error_category")
                if category is None and status_holder["status"] >= 500:
                    category = ErrorCategory.INTERNAL_ERROR
                rec = RequestRecord(
                    request_id=request_id,
                    endpoint=self._endpoint(scope),
                    status_code=status_holder["status"],
                    latency_ms=elapsed_ms,
                    error_category=category,
                    timestamp=time.time(),
                )
                for event in self.metrics.record(rec):
                    logger.warning("alert raised", extra=event.as_dict())
        finally:
            reset_request_id(token)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _wrapped_send(send: Send, request_id: str) -> Send:
        """在响应头中注入/覆盖 X-Request-ID。"""

        async def sender(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.decode("latin1").lower() != REQUEST_ID_HEADER.lower()
                ]
                headers.append(
                    (
                        REQUEST_ID_HEADER.lower().encode("latin1"),
                        request_id.encode("latin1"),
                    )
                )
                message["headers"] = headers
            await send(message)

        return sender

    @staticmethod
    def _endpoint(scope: Scope) -> str:
        """优先使用路由模板（含参数也聚合到同一端点），否则标记 unmatched。"""
        route = scope.get("route")
        path = getattr(route, "path", None)
        if path:
            return path
        return UNMATCHED_ENDPOINT

    def _log_completion(
        self, scope: Scope, request_id: str, generated: bool,
        status: int, elapsed_ms: float,
    ) -> None:
        logger.info(
            "request completed",
            extra={
                "method": scope.get("method"),
                "path": scope.get("path"),
                "endpoint": self._endpoint(scope),
                "status_code": status,
                "latency_ms": round(elapsed_ms, 3),
                "request_id_generated": generated,
            },
        )

    async def _record(
        self, scope: Scope, response: JSONResponse, request_id: str,
        endpoint: str, elapsed_ms: float,
        category: ErrorCategory | None = None,
    ) -> None:
        if self.metrics is None:
            return
        if category is None and response.status_code >= 500:
            category = ErrorCategory.INTERNAL_ERROR
        rec = RequestRecord(
            request_id=request_id,
            endpoint=endpoint,
            status_code=response.status_code,
            latency_ms=elapsed_ms,
            error_category=category,
            timestamp=time.time(),
        )
        for event in self.metrics.record(rec):
            logger.warning("alert raised", extra=event.as_dict())
