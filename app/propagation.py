"""跨服务链路传播：把本次请求标识透传给下游，并回收下游标识。

行为：
- 默认开启，业务代码发起下游调用前用 :func:`outbound_headers` 拿到可原样
  携带的请求头（``{头名: 本次请求标识}``），下游即可串到同一条链路；
- 可经配置或 :func:`set_propagation_scope` 关闭（如调第三方不外发），
  关闭时返回空头且不携带任何标识；
- 下游响应若回传它那一次的标识，用 :func:`capture_downstream_id` 落到
  本请求上下文，随结构化记录关联回溯；
- 所有状态都绑定在 ``ContextVar`` 上，asyncio 按任务复制上下文，
  并发请求各归各，绝不互相串。
"""

from __future__ import annotations

import json
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Mapping

from .config import get_settings
from .request_id import get_request_id

#: 本请求是否允许出站传播（None 表示跟随全局配置）
_enabled_var: ContextVar[bool | None] = ContextVar("propagation_enabled", default=None)

#: 本请求关联到的下游响应标识
_downstream_id_var: ContextVar[str] = ContextVar("downstream_request_id", default="")


@dataclass(frozen=True)
class DownstreamCall:
    """一次下游调用的关联信息（用于结构化记录与回溯）。"""

    downstream_request_id: str


def _header_name() -> str:
    return get_settings().propagation.header_name


def set_propagation_scope(enabled: bool) -> Token[bool | None]:
    """在当前请求上下文内覆盖出站开关（返回 reset 令牌）。"""
    return _enabled_var.set(enabled)


def reset_propagation_scope(token: Token[bool | None]) -> None:
    _enabled_var.reset(token)


def propagation_enabled() -> bool:
    """当前请求实际生效的出站开关：上下文覆盖优先，否则跟随全局配置。"""
    override = _enabled_var.get()
    if override is not None:
        return override
    return get_settings().propagation.enabled


def outbound_headers(
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """返回发起下游调用时可原样携带的请求头。

    - 开关关闭、或当前不在请求上下文（无标识）时：不携带传播头；
    - ``extra`` 中的业务自定义头原样合并，但不允许覆盖传播头。
    """
    headers: dict[str, str] = dict(extra or {})
    if not propagation_enabled():
        return headers
    request_id = get_request_id()
    if request_id:
        headers[_header_name()] = request_id
    return headers


def capture_downstream_id(
    response: Any = None,
    *,
    headers: Mapping[str, str] | None = None,
) -> str:
    """从下游响应中回收它那一次的标识并绑定到本请求上下文。

    支持 httpx 响应对象（``response.headers``）或直接传入响应头映射；
    下游未回传时返回空串且不改变关联状态。返回回收结果便于业务断言/日志。
    """
    raw_headers: Mapping[str, str] | None = headers
    if raw_headers is None and response is not None:
        raw_headers = getattr(response, "headers", None)
    downstream_id = ""
    if raw_headers:
        wanted = _header_name().lower()
        for key, value in raw_headers.items():
            if key.lower() == wanted:
                downstream_id = value
                break
    if downstream_id:
        _downstream_id_var.set(downstream_id)
    return downstream_id


def get_downstream_id() -> str:
    """读取本请求已回收的下游标识（未回收为空串）。"""
    return _downstream_id_var.get()


def reset_request_scope() -> None:
    """请求结束时清理下游标识（随上下文销毁，防止线程池复用残留）。"""
    _downstream_id_var.set("")


def downstream_headers_snapshot() -> str | None:
    """供结构化记录使用的下游调用 JSON 快照；无下游标识时为 None。"""
    downstream_id = get_downstream_id()
    if not downstream_id:
        return None
    return json.dumps({"downstream_request_id": downstream_id}, ensure_ascii=False)
