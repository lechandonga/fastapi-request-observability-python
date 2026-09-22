"""FastAPI 应用入口：组装请求级可观测性能力。

* :class:`~app.middleware.ObservabilityMiddleware` 负责标识、耗时、兜底异常；
* 本文件注册的异常处理器负责把 *预期内* 的异常转成统一安全响应，
  并把错误类型经 ``scope`` 回传给中间件，用于指标的错误类型细分；
* 注册表挂在 ``app.state.metrics``，测试可通过 :func:`create_app` 注入
  独立实例，避免用例间互相污染。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.context import REQUEST_ID_HEADER, get_request_id
from app.errors import (
    ErrorCategory,
    ObservabilityError,
    error_body,
    status_for_category,
)
from app.logging_setup import configure_logging
from app.metrics import MetricsRegistry, registry
from app.middleware import ObservabilityMiddleware
from app.routes import router

configure_logging()
logger = logging.getLogger("app.http")

#: 处理器 -> 中间件 传递已归类错误类型所用的 scope 键。
SCOPE_ERROR_CATEGORY = "obs_error_category"


def create_app(metrics: MetricsRegistry | None = None) -> FastAPI:
    """构造应用；可注入独立指标注册表（主要用于测试隔离）。"""
    app = FastAPI()
    reg = metrics if metrics is not None else registry
    app.state.metrics = reg
    app.add_middleware(ObservabilityMiddleware, metrics=reg)
    app.include_router(router)
    _register_exception_handlers(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ObservabilityError)
    async def observability_error_handler(
        request: Request, exc: ObservabilityError
    ) -> JSONResponse:
        # 业务自定义异常：分类是显式的，消息是子类提供的安全文案。
        request.scope[SCOPE_ERROR_CATEGORY] = exc.category
        rid = get_request_id() or ""
        logger.warning(
            "handled observability error",
            extra={"error_category": exc.category.value, "path": request.url.path},
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.category, exc.public_message, rid),
            headers={REQUEST_ID_HEADER: rid},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        rid = get_request_id() or ""
        # 仅 404 有对应分类；其余 HTTP 状态码按原样返回，错误类型留空
        # （调用方错误不纳入告警，只按状态码计入指标）。
        category = (
            ErrorCategory.NOT_FOUND if exc.status_code == 404 else None
        )
        if category is not None:
            request.scope[SCOPE_ERROR_CATEGORY] = category
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(
                category or ErrorCategory.INTERNAL_ERROR,
                str(exc.detail) if exc.status_code < 500 else "internal server error",
                rid,
            ),
            headers={REQUEST_ID_HEADER: rid},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 参数校验细节来自调用方输入，可安全返回；不包含任何内部路径信息。
        rid = get_request_id() or ""
        request.scope[SCOPE_ERROR_CATEGORY] = ErrorCategory.VALIDATION_ERROR
        body = error_body(
            ErrorCategory.VALIDATION_ERROR, "request validation failed", rid
        )
        body["error"]["details"] = exc.errors()
        return JSONResponse(
            status_code=status_for_category(ErrorCategory.VALIDATION_ERROR),
            content=body,
            headers={REQUEST_ID_HEADER: rid},
        )


app = create_app()
