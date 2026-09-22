"""异常处理注册：统一错误信封，同时保持框架默认状态码兼容。

- 404 等 HTTPException -> not_found 信封（状态码沿用原值）；
- 请求参数校验错误 -> validation_error(422)，detail 仅含安全的字段信息；
- 已知业务异常（ObservabilityError 子类）-> 各自归类状态码，
  内部异常文本只进日志不进响应；
- 其余未捕获异常由中间件兜底归类。
"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from .errors import (
    ErrorType,
    ObservabilityError,
    STATUS_BY_ERROR_TYPE,
    build_error_body,
)
from .logging_setup import diagnostic_logger
from .request_id import get_request_id


def _error_response(
    request: Request,
    status_code: int,
    error_type: ErrorType,
    *,
    detail: object | None = None,
    message: str | None = None,
    header_name: str = "x-request-id",
) -> Response:
    # 把归类结果回写 scope，供中间件计入 by_error_type 维度
    request.scope["obs.error_type"] = error_type.value
    request_id = get_request_id() or response_rid(request, header_name)
    body = build_error_body(
        request_id,
        error_type,
        message=message,
        detail=detail if isinstance(detail, (str, type(None))) else json_safe(detail),
    )
    return Response(
        content=json.dumps(body, ensure_ascii=False),
        status_code=status_code,
        media_type="application/json",
        headers={header_name: request_id} if request_id else None,
    )


def response_rid(request: Request, header_name: str) -> str:
    return request.headers.get(header_name, "")


def json_safe(value: object) -> object:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def register_exception_handlers(app: FastAPI, header_name: str = "x-request-id") -> None:
    h = header_name.lower()

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            et = ErrorType.NOT_FOUND
            detail: object | None = "路径或资源不存在"
        elif exc.status_code == 405:
            et = ErrorType.VALIDATION_ERROR
            detail = "方法不被允许"
        elif exc.status_code == 429:
            et = ErrorType.RATE_LIMITED
            detail = None
        else:
            et = ErrorType.BUSINESS_ERROR
            detail = None
        return _error_response(
            request, exc.status_code, et, detail=detail, header_name=h
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> Response:
        # 仅暴露字段名与校验类型，不暴露内部值/堆栈
        safe_errors = [
            {
                "field": ".".join(str(p) for p in err.get("loc", []) if p != "body"),
                "rule": err.get("type", "value_error"),
            }
            for err in exc.errors()
        ]
        return _error_response(
            request,
            STATUS_BY_ERROR_TYPE[ErrorType.VALIDATION_ERROR],
            ErrorType.VALIDATION_ERROR,
            detail=safe_errors or None,
            header_name=h,
        )

    @app.exception_handler(ObservabilityError)
    async def observability_error_handler(
        request: Request, exc: ObservabilityError
    ) -> Response:
        et = exc.error_type
        # 内部异常文本仅落诊断日志
        diagnostic_logger.warning(
            "业务异常已归类",
            extra={"error_type": et.value, "exception_type": type(exc).__name__},
            exc_info=True,
        )
        return _error_response(
            request,
            STATUS_BY_ERROR_TYPE[et],
            et,
            header_name=h,
        )
