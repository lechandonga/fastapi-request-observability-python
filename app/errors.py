"""错误分类与对外错误响应。

错误响应信封固定为::

    {"request_id": ..., "error": {"type": ..., "message": ..., "detail": ...}}

内部堆栈/异常字符串绝不进入响应体，只写入诊断日志；
``detail`` 仅承载安全、可解释的信息（如校验字段、非法原因）。
"""

from __future__ import annotations

from enum import Enum
from dataclasses import dataclass


class ErrorType(str, Enum):
    INVALID_REQUEST_ID = "invalid_request_id"
    VALIDATION_ERROR = "validation_error"
    NOT_FOUND = "not_found"
    DEPENDENCY_ERROR = "dependency_error"
    BUSINESS_ERROR = "business_error"
    RATE_LIMITED = "rate_limited"
    TIMEOUT_ERROR = "timeout_error"
    NETWORK_SYSTEM_ERROR = "network_system_error"
    UNCAUGHT_ERROR = "uncaught_error"


# 错误类型 -> HTTP 状态码
STATUS_BY_ERROR_TYPE: dict[ErrorType, int] = {
    ErrorType.INVALID_REQUEST_ID: 400,
    ErrorType.VALIDATION_ERROR: 422,
    ErrorType.NOT_FOUND: 404,
    ErrorType.BUSINESS_ERROR: 422,
    ErrorType.RATE_LIMITED: 429,
    ErrorType.TIMEOUT_ERROR: 504,
    ErrorType.DEPENDENCY_ERROR: 502,
    # 网络/系统级故障（连接被拒、连接中断、底层系统调用失败等）
    ErrorType.NETWORK_SYSTEM_ERROR: 502,
    ErrorType.UNCAUGHT_ERROR: 500,
}

# 对外固定安全文案：不暴露实现细节
SAFE_MESSAGE_BY_ERROR_TYPE: dict[ErrorType, str] = {
    ErrorType.INVALID_REQUEST_ID: "请求标识非法",
    ErrorType.VALIDATION_ERROR: "请求参数校验失败",
    ErrorType.NOT_FOUND: "资源不存在",
    ErrorType.BUSINESS_ERROR: "业务处理失败",
    ErrorType.RATE_LIMITED: "请求过于频繁",
    ErrorType.TIMEOUT_ERROR: "上游处理超时",
    ErrorType.DEPENDENCY_ERROR: "上游依赖异常",
    ErrorType.NETWORK_SYSTEM_ERROR: "网络或系统错误",
    ErrorType.UNCAUGHT_ERROR: "服务内部错误",
}

# 超时异常类名/模块片段 -> 错误类型
_TIMEOUT_TYPE_NAMES = {"TimeoutError", "asyncio.TimeoutError", "ReadTimeout"}

#: 网络/系统级故障异常类名（连接被拒、连接中断、DNS、系统调用失败等）。
#: 仅匹配来自标准库/常见网络库的具体异常名，普通业务异常不会误伤。
_NETWORK_SYSTEM_TYPE_NAMES = {
    # socket / 内置 OSError 子类
    "ConnectionError",
    "ConnectionRefusedError",
    "ConnectionResetError",
    "ConnectionAbortedError",
    "BrokenPipeError",
    "InterruptedError",
    "gaierror",
    "herror",
    "SSLError",
    "SSLEOFError",
    # http.client / urllib3 的连接与协议中断
    "RemoteDisconnected",
    "ProtocolError",
    "MaxRetryError",
    "NewConnectionError",
    # asyncio 传输层
    "ClientConnectionError",
    "ClientConnectorError",
    "ClientConnectorDNSError",
    "ClientConnectorSSLError",
    "ClientConnectorCertificateError",
    "ServerDisconnectedError",
}


class ObservabilityError(Exception):
    """可观测体系可识别的业务异常基类（内部异常文本不对外）。"""

    error_type: ErrorType = ErrorType.BUSINESS_ERROR


class BusinessError(ObservabilityError):
    error_type = ErrorType.BUSINESS_ERROR


class DependencyError(ObservabilityError):
    error_type = ErrorType.DEPENDENCY_ERROR


class RateLimitedError(ObservabilityError):
    error_type = ErrorType.RATE_LIMITED


# 异常类名 -> 错误类型（未知模块抛出的同名异常也按此归类）
_CLASSIFICATION_BY_TYPE: dict[str, ErrorType] = {
    cls.__name__: cls.error_type
    for cls in (DependencyError, BusinessError, RateLimitedError)
}


@dataclass(frozen=True)
class ErrorResponse:
    request_id: str
    error_type: ErrorType
    message: str
    detail: str | None
    status_code: int


def classify_exception(exc: BaseException) -> tuple[ErrorType, int, str]:
    """把未捕获异常归类为可区分的错误类型。

    返回 ``(错误类型, HTTP 状态码, 对外安全消息)``。
    归类依据（按优先级）：
      1. 已知业务异常基类（见 app.errors 兼容，或路由模块自定义）；
      2. 超时类异常名 -> timeout_error(504)（含内置 TimeoutError）；
      3. 内置连接类异常 ``ConnectionError``（含 ConnectionRefusedError/
         ConnectionResetError/BrokenPipeError 等）或网络库具体连接/系统级
         异常名 -> network_system_error(502)；普通文件类 OSError
         （如 FileNotFoundError）不在此列，仍归 uncaught_error；
      4. 其余一律 uncaught_error(500)。
    """
    name = type(exc).__name__
    if isinstance(exc, ObservabilityError):
        et = exc.error_type
    elif name in _CLASSIFICATION_BY_TYPE:
        et = _CLASSIFICATION_BY_TYPE[name]
    elif name in _TIMEOUT_TYPE_NAMES or "timeout" in name.lower():
        et = ErrorType.TIMEOUT_ERROR
    elif isinstance(exc, ConnectionError) or name in _NETWORK_SYSTEM_TYPE_NAMES:
        et = ErrorType.NETWORK_SYSTEM_ERROR
    else:
        et = ErrorType.UNCAUGHT_ERROR
    return et, STATUS_BY_ERROR_TYPE[et], SAFE_MESSAGE_BY_ERROR_TYPE[et]


def status_for_error_type(error_type: ErrorType) -> int:
    return STATUS_BY_ERROR_TYPE[error_type]


def build_error_body(
    request_id: str,
    error_type: ErrorType,
    message: str | None = None,
    detail: str | None = None,
) -> dict:
    """构造不泄露内部实现的错误响应体。"""
    return {
        "request_id": request_id,
        "error": {
            "type": error_type.value,
            "message": message or SAFE_MESSAGE_BY_ERROR_TYPE[error_type],
            "detail": detail,
        },
    }
