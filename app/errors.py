"""错误分类与不泄露内部细节的安全错误响应。

所有未捕获异常都会先被归类为某个 :class:`ErrorCategory`，再对外返回统一
结构的响应体::

    {
      "error": {
        "category": "internal_error",   # 可区分、可统计的错误类型
        "message": "internal server error",  # 面向调用方的安全描述
        "request_id": "req-..."         # 排查时用于关联日志的诊断标识
      }
    }

内部堆栈只写进服务端日志（带 request_id），绝不出现在响应体中。
"""
from __future__ import annotations

from enum import Enum

#: 安全兜底文案：不包含任何实现细节。
INTERNAL_ERROR_MESSAGE = "internal server error"


class ErrorCategory(str, Enum):
    """可区分的错误类型（枚举值即对外可见的稳定字符串）。"""

    #: 调用方提供的 X-Request-ID 非法 -> 400
    INVALID_REQUEST_ID = "invalid_request_id"
    #: 请求体/参数校验失败 -> 422
    VALIDATION_ERROR = "validation_error"
    #: 资源不存在 -> 404
    NOT_FOUND = "not_found"
    #: 依赖的下游服务失败 -> 502
    UPSTREAM_ERROR = "upstream_error"
    #: 依赖的下游服务超时 -> 504
    TIMEOUT = "timeout"
    #: 未归类的服务端内部错误 -> 500
    INTERNAL_ERROR = "internal_error"


class ObservabilityError(Exception):
    """带错误分类的业务异常基类。"""

    category: ErrorCategory = ErrorCategory.INTERNAL_ERROR
    status_code: int = 500
    #: 面向调用方的安全消息；子类可覆盖。
    public_message: str = INTERNAL_ERROR_MESSAGE

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.public_message)
        if message:
            self.public_message = message


class InvalidRequestIdError(ObservabilityError):
    category = ErrorCategory.INVALID_REQUEST_ID
    status_code = 400
    public_message = "invalid request id"


class UpstreamError(ObservabilityError):
    category = ErrorCategory.UPSTREAM_ERROR
    status_code = 502
    public_message = "upstream service error"


class UpstreamTimeoutError(ObservabilityError):
    category = ErrorCategory.TIMEOUT
    status_code = 504
    public_message = "upstream service timeout"


#: 错误类型 -> HTTP 状态码（归类与映射的唯一事实来源）。
CATEGORY_STATUS: dict[ErrorCategory, int] = {
    ErrorCategory.INVALID_REQUEST_ID: 400,
    ErrorCategory.VALIDATION_ERROR: 422,
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.UPSTREAM_ERROR: 502,
    ErrorCategory.TIMEOUT: 504,
    ErrorCategory.INTERNAL_ERROR: 500,
}


def classify_exception(exc: BaseException) -> ErrorCategory:
    """把任意异常归类为可区分的错误类型。

    判定依据（顺序敏感）：

    1. :class:`ObservabilityError` 取其自带分类；
    2. ``TimeoutError``（含 asyncio.TimeoutError，3.11 起互为别名）
       归类为 ``timeout``；
    3. ``ConnectionError`` / ``OSError`` 归类为 ``upstream_error``
       （代表外部依赖的传输层故障）；
    4. 其余一律 ``internal_error``，避免暴露未预期的内部细节。
    """
    if isinstance(exc, ObservabilityError):
        return exc.category
    if isinstance(exc, (TimeoutError, asyncio_timeout_error())):
        return ErrorCategory.TIMEOUT
    if isinstance(exc, (ConnectionError, OSError)):
        return ErrorCategory.UPSTREAM_ERROR
    return ErrorCategory.INTERNAL_ERROR


def asyncio_timeout_error() -> type:
    """返回 asyncio.TimeoutError；在旧版本上回退为 TimeoutError。"""
    import asyncio

    return getattr(asyncio, "TimeoutError", TimeoutError)


def status_for_category(category: ErrorCategory) -> int:
    """返回错误类型对应的 HTTP 状态码。"""
    return CATEGORY_STATUS.get(category, 500)


def public_message_for(exc: BaseException, category: ErrorCategory) -> str:
    """返回可对外展示的消息。

    已分类的业务异常使用其自带安全消息；内部错误统一使用兜底文案，
    防止把 ``KeyError: 'xxx'`` 等实现细节直接回传给调用方。
    """
    if isinstance(exc, ObservabilityError):
        return exc.public_message
    if category == ErrorCategory.INTERNAL_ERROR:
        return INTERNAL_ERROR_MESSAGE
    # 非 ObservabilityError 的传输/超时类异常也只给类别化描述。
    if category == ErrorCategory.TIMEOUT:
        return "upstream service timeout"
    if category == ErrorCategory.UPSTREAM_ERROR:
        return "upstream service error"
    return INTERNAL_ERROR_MESSAGE


def error_body(category: ErrorCategory, message: str, request_id: str) -> dict:
    """构造统一结构的安全错误响应体。"""
    return {
        "error": {
            "category": category.value,
            "message": message,
            "request_id": request_id,
        }
    }
