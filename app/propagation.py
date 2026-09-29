"""跨服务链路传播：出站请求头构造与下游标识回收。

使用方式（业务代码发起下游调用之前）::

    headers = build_outbound_headers()          # 默认开关开 -> 带本请求标识
    resp = await http_client.get(url, headers=headers)
    downstream_id = capture_downstream_response(resp.headers)

- 标识与下游回链标识都存于 :class:`ContextVar`，asyncio 每个任务复制
  context，并发请求各归各，绝不互串；
- 全局开关默认开启，可经配置/环境变量关闭；单次调用也可 ``enabled=False``
  覆盖（例如调第三方时不外发）；
- 关闭或在请求上下文外调用时，返回空头、不抛异常。
"""

from __future__ import annotations

from contextvars import ContextVar

from .config import RequestIdSettings, get_settings
from .request_id import get_request_id

#: 本请求已回收到的下游标识，按下游名（未命名为 ""）各存一份
#: 默认用 None 哨兵（不用可变 dict 作为 ContextVar 默认值，避免共享状态）
_downstream_ids_var: ContextVar[dict[str, str] | None] = ContextVar(
    "downstream_request_ids", default=None
)


def build_outbound_headers(
    *,
    downstream: str = "",
    enabled: bool | None = None,
    settings: RequestIdSettings | None = None,
) -> dict[str, str]:
    """构造可原样带给下游的请求头。

    - 全局开关关闭，或本次显式 ``enabled=False`` -> 返回 ``{}``（不外发）；
    - 请求上下文外（无标识）-> 返回 ``{}``；
    - 其余情况返回 ``{出站头名: 本次请求标识}``。

    ``downstream`` 仅用于说明目标下游名，不参与头构造。
    """
    cfg = settings or get_settings().request_id
    propagation_on = cfg.outbound_propagation_enabled if enabled is None else enabled
    request_id = get_request_id()
    if not propagation_on or not request_id:
        return {}
    return {cfg.outbound_header_name: request_id}


def capture_downstream_response(
    headers,
    *,
    downstream: str = "",
    settings: RequestIdSettings | None = None,
) -> str | None:
    """从下游响应头读取其本次标识，绑定到当前请求上下文并返回。

    ``headers`` 可以是任意大小写不敏感的映射（如 ``httpx.Headers``、
    ``starlette.datastructures.Headers`` 或普通 dict）。
    缺失/空白时返回 ``None``，且不写入上下文。
    """
    cfg = settings or get_settings().request_id
    raw = _header_get(headers, cfg.downstream_id_header_name)
    if raw is None or raw.strip() == "":
        return None
    downstream_id = raw.strip()
    current = dict(_downstream_ids_var.get() or {})
    current[downstream] = downstream_id
    _downstream_ids_var.set(current)
    return downstream_id


def record_downstream_id(downstream_id: str, downstream: str = "") -> str:
    """直接登记一个下游标识（非 HTTP 场景或测试用），返回该标识。"""
    current = dict(_downstream_ids_var.get() or {})
    current[downstream] = downstream_id
    _downstream_ids_var.set(current)
    return downstream_id


def get_downstream_ids() -> dict[str, str]:
    """当前请求已回收的下游标识快照（下游名 -> 标识）。"""
    return dict(_downstream_ids_var.get() or {})


def reset_downstream_ids() -> None:
    """请求结束时清空，防止上下文复用造成的残留。"""
    _downstream_ids_var.set({})


def _header_get(headers, name: str) -> str | None:
    # httpx / starlette Headers：不敏感映射
    try:
        value = headers.get(name)
        if value is not None:
            return value
    except TypeError:
        pass
    # 普通 dict（键大小写不定）
    if hasattr(headers, "items"):
        wanted = name.lower()
        for key, value in headers.items():
            if key.lower() == wanted:
                return value
    return None
