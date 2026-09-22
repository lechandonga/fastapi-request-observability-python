"""请求关联标识（Request ID）的生成、校验与传播。

规则（确定性、可解释）：

* 调用方通过 ``X-Request-ID`` 请求头传入标识；
* 合法字符为字母、数字、下划线、短横线，首字符必须是字母或数字，
  总长度 8~128；
* 未提供（头缺失或为空）时，服务端按 ``req-<uuid4 的 32 个十六进制字符>``
  生成，全局唯一且可从字面前缀识别为服务端生成；
* 提供了非法标识时不做静默替换，由上层拒绝（400）；
* 标识通过 :data:`_request_id_var` 这个 ``ContextVar`` 保存，
  ``asyncio`` 会为每个任务复制上下文，因此并发请求之间天然隔离；
* 内部调用使用 :func:`downstream_headers` 把当前标识透传给下游。
"""
from __future__ import annotations

import re
import uuid
from contextvars import ContextVar, Token

#: 接收/回写请求标识所用的 HTTP 头名称。
REQUEST_ID_HEADER = "X-Request-ID"

#: 合法标识：8~128 位，首字符字母/数字，其余可含 _ - 。
#: 该规则同时可抵御换行注入（非法字符无法进入日志行与响应头）。
_VALID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")

#: 服务端生成标识的固定前缀。
GENERATED_PREFIX = "req-"

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """返回当前上下文中的请求标识；未设置时为 ``None``。"""
    return _request_id_var.get()


def set_request_id(request_id: str) -> Token[str | None]:
    """把请求标识绑定到当前上下文，返回可用于复位的 token。"""
    return _request_id_var.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    """恢复 ContextVar 到 :func:`set_request_id` 之前的值。"""
    _request_id_var.reset(token)


def is_valid_request_id(value: str) -> bool:
    """判断调用方提供的标识是否合法。

    判定依据：

    1. 必须是 ``str`` 且非空白；
    2. 长度 8~128；
    3. 首字符为字母或数字；
    4. 其余字符只能是字母、数字、下划线或短横线。
    """
    if not isinstance(value, str) or not value.strip():
        return False
    return bool(_VALID_PATTERN.match(value))


def generate_request_id() -> str:
    """按确定性格式生成服务端请求标识：``req-<uuid4 hex>``。

    uuid4 为 122 位随机数，碰撞概率可忽略；格式固定便于检索与区分来源。
    """
    return f"{GENERATED_PREFIX}{uuid.uuid4().hex}"


def resolve_request_id(raw: str | None) -> tuple[str, bool]:
    """解析入站标识。

    返回 ``(request_id, was_generated)``：

    * ``raw`` 缺失/为空 -> 生成新标识，``was_generated=True``；
    * ``raw`` 合法 -> 原样采用（不做大小写折叠），``was_generated=False``；
    * ``raw`` 非法 -> 抛出 :class:`~app.errors.InvalidRequestIdError`。
    """
    from .errors import InvalidRequestIdError

    if raw is None or not raw.strip():
        return generate_request_id(), True
    value = raw.strip()
    if not is_valid_request_id(value):
        raise InvalidRequestIdError(
            f"invalid {REQUEST_ID_HEADER}: must be 8-128 chars of "
            "letters/digits/_/- and start with a letter or digit"
        )
    return value, False


def downstream_headers(request_id: str | None = None) -> dict[str, str]:
    """构造下游内部调用应携带的请求头，用于标识跨调用传播。

    未显式传入时取当前上下文的标识；上下文也没有则返回空映射，
    由下游按其自身规则生成。
    """
    rid = request_id if request_id is not None else get_request_id()
    if rid is None:
        return {}
    return {REQUEST_ID_HEADER: rid}
