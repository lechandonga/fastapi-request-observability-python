"""可观测性 ASGI 中间件。

职责：
1. 接收 ``X-Request-ID``：缺失生成、非法直接 400 拒绝（确定性规则）；
2. 通过 ``ContextVar`` 在本请求内传播标识，并回写到响应头；
3. 包装下游处理，捕获未处理异常 -> 归类 -> 统一安全错误信封，
   诊断细节（含堆栈）只落日志；
4. 计时并产出 :class:`RequestRecord` 交给指标注册器，记录告警事件。
"""

from __future__ import annotations

import json
import time
import logging
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .errors import ErrorType, STATUS_BY_ERROR_TYPE, classify_exception, build_error_body
from .logging_setup import access_logger, diagnostic_logger
from .metrics import MetricsRegistry, RequestRecord
from .request_id import (
    InvalidRequestId,
    resolve_request_id,
    set_request_id,
    reset_request_id,
)

_UNMATCHED_ENDPOINT = "__unmatched__"
_INVALID_ID_ENDPOINT = "__invalid_request_id__"
_log = logging.getLogger("observability.middleware")


class ObservabilityMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        registry: MetricsRegistry,
        header_name: str,
    ) -> None:
        self.app = app
        self._registry = registry
        # ASGI 头键必须为小写 bytes
        self._header_bytes = header_name.lower().encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        started = time.perf_counter()
        started_wall = time.time()
        raw = self._extract_header(scope)
        request_id, invalid = resolve_request_id(raw)
        if invalid is not None:
            await self._send_invalid_id(scope, send, invalid)
            # 被拒请求同样计入结构化记录（固定端点桶，request_id 留空）
            self._registry.record(
                RequestRecord(
                    request_id="",
                    endpoint=_INVALID_ID_ENDPOINT,
                    method=method,
                    status_code=STATUS_BY_ERROR_TYPE[ErrorType.INVALID_REQUEST_ID],
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    error_type=ErrorType.INVALID_REQUEST_ID,
                    timestamp=started_wall,
                )
            )
            return

        # 绑定到本请求的上下文；中间件 await 链中的内部调用读取的都是该值
        token = set_request_id(request_id)
        status_code = 500
        error_type: ErrorType | None = None
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message.get("status", 500))
                # 标识回传，贯穿日志/错误响应/内部调用
                headers = message.get("headers")
                if headers is None:
                    headers = []
                headers = [
                    (k, v) for (k, v) in headers if k.lower() != self._header_bytes
                ]
                headers.append((self._header_bytes, request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:  # noqa: BLE001 - 边界处统一归类所有未捕获异常
            error_type, status_code, safe_message = classify_exception(exc)
            # 诊断信息（含完整堆栈）仅入日志
            diagnostic_logger.error(
                "未捕获异常已归类",
                extra={
                    "error_type": error_type.value,
                    "exception_type": type(exc).__name__,
                    "path": scope.get("path"),
                    "method": method,
                },
                exc_info=True,
            )
            # 响应头已发出后无法再改写状态码/响应体：只记录与归类，不二次发送
            if not response_started:
                await self._send_error(
                    send,
                    request_id=request_id,
                    status_code=status_code,
                    error_type=error_type,
                    message=safe_message,
                )
            else:
                raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            endpoint = self._endpoint_name(scope)
            # 下游异常处理器已把归类结果写入 scope；未捕获异常来自 except 分支。
            # 必须先解析出最终 error_type，再构造 RequestRecord。
            if error_type is None:
                scoped_type = scope.get("obs.error_type")
                if scoped_type:
                    try:
                        error_type = ErrorType(scoped_type)
                    except ValueError:
                        error_type = None
            # 400 非法标识在前面提前 return，不计入端点指标；此处记录所有进入处理的请求
            record = RequestRecord(
                request_id=request_id,
                endpoint=endpoint,
                method=method,
                status_code=status_code,
                duration_ms=duration_ms,
                error_type=error_type,
                timestamp=started_wall,
            )
            alerts = self._registry.record(record)
            for event in alerts:
                _log.warning("告警事件: %s", event.message)
            access_logger.info(
                "request",
                extra={
                    "endpoint": endpoint,
                    "method": method,
                    "status_code": status_code,
                    "duration_ms": round(duration_ms, 3),
                    "error_type": error_type.value if error_type else None,
                    "alerts": [e.alert_id for e in alerts],
                },
            )
            reset_request_id(token)

    # ------------------------------------------------------------------ #
    def _extract_header(self, scope: Scope) -> str | None:
        for key, value in scope.get("headers", []):
            if key == self._header_bytes:
                return value.decode("latin-1")
        return None

    @staticmethod
    def _endpoint_name(scope: Scope) -> str:
        route = scope.get("route")
        if route is not None and getattr(route, "path", None):
            return route.path
        # 未匹配路由（404 等）归入固定桶，避免高基数
        return _UNMATCHED_ENDPOINT

    async def _send_invalid_id(self, scope: Scope, send: Send, invalid: InvalidRequestId) -> None:
        # 非法标识不生成上下文标识，响应头按规则不带该标识
        body = {
            "request_id": None,
            "error": {
                "type": ErrorType.INVALID_REQUEST_ID.value,
                "message": "请求标识非法",
                "detail": invalid.reason,
            },
        }
        await self._json_response(
            send,
            STATUS_BY_ERROR_TYPE[ErrorType.INVALID_REQUEST_ID],
            body,
            request_id=None,
        )
        access_logger.warning(
            "rejected invalid request id",
            extra={"path": scope.get("path"), "reason": invalid.reason},
        )

    async def _send_error(
        self,
        send: Send,
        *,
        request_id: str,
        status_code: int,
        error_type: ErrorType,
        message: str,
    ) -> None:
        body = build_error_body(request_id, error_type, message)
        await self._json_response(send, status_code, body, request_id=request_id)

    async def _json_response(
        self,
        send: Send,
        status_code: int,
        body: dict[str, Any],
        *,
        request_id: str | None,
    ) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = [(b"content-type", b"application/json; charset=utf-8")]
        if request_id is not None:
            headers.append((self._header_bytes, request_id.encode("latin-1")))
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": payload,
                "more_body": False,
            }
        )
