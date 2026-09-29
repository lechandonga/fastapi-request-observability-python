"""请求关联标识：校验、生成、上下文传播。

规则：
- 调用方通过 ``X-Request-ID`` 头传入标识；
- 缺失时按固定规则生成 ``req-<uuid4-hex>``（前缀固定，长度确定，并发不碰撞）；
- 非法时由调用方决定拒绝（中间件返回 400）；
- 标识存于 ``ContextVar``，asyncio 任务/线程之间互不串扰。
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass

from .config import RequestIdSettings, get_settings

_INVALID_REASON_EMPTY = "request_id 不能为空"
_INVALID_REASON_LENGTH = "request_id 超过最大长度"
_INVALID_REASON_PATTERN = "request_id 包含非法字符"


@dataclass(frozen=True)
class InvalidRequestId:
    """非法标识判定结果。"""

    reason: str
    raw: str


# 当前请求标识的上下文变量；每个 asyncio 任务复制 context，天然隔离并发请求。
_request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def validate_request_id(raw: str, settings: RequestIdSettings | None = None) -> str | None:
    """合法则原样返回，非法返回 None。"""
    cfg = settings or get_settings().request_id
    if len(raw) == 0 or len(raw) > cfg.max_length:
        return None
    # 配置中的模式已带 ^...$ 锚点，去掉后交给 re.fullmatch
    pattern = cfg.valid_pattern
    if pattern.startswith("^"):
        pattern = pattern[1:]
    if pattern.endswith("$"):
        pattern = pattern[:-1]
    if re.fullmatch(pattern, raw) is None:
        return None
    return raw


def _pattern_to_fullmatch(cfg: RequestIdSettings) -> str:
    p = cfg.valid_pattern
    if p.startswith("^"):
        p = p[1:]
    if p.endswith("$"):
        p = p[:-1]
    return p


def invalid_reason(raw: str, settings: RequestIdSettings | None = None) -> str:
    """返回非法原因（用于 400 响应与日志）。"""
    cfg = settings or get_settings().request_id
    if len(raw) == 0:
        return _INVALID_REASON_EMPTY
    if len(raw) > cfg.max_length:
        return f"{_INVALID_REASON_LENGTH}（{len(raw)} > {cfg.max_length}）"
    return _INVALID_REASON_PATTERN


def generate_request_id(settings: RequestIdSettings | None = None) -> str:
    """按确定规则生成新标识：固定前缀 + uuid4 的 32 位十六进制。"""
    cfg = settings or get_settings().request_id
    return f"{cfg.prefix}{uuid.uuid4().hex}"


def generate_rejected_request_id(settings: RequestIdSettings | None = None) -> str:
    """为入口即被挡的请求生成本次专属标识（``rej-`` 前缀）。

    语义与正常生成标识不同（专属前缀），便于在日志/明细里一眼区分，
    但同样保证并发不碰撞；该标识不通过 ``X-Request-ID`` 响应头回传。
    """
    cfg = settings or get_settings().request_id
    return f"{cfg.rejected_prefix}{uuid.uuid4().hex}"


def resolve_request_id(
    raw: str | None, settings: RequestIdSettings | None = None
) -> tuple[str | None, InvalidRequestId | None]:
    """把入站头解析为 (标识, 非法结果)。

    - 缺失（None 或空白）-> 生成新标识，错误为 None；
    - 合法 -> 原样返回，错误为 None；
    - 非法 -> 标识为 None，附带非法原因。
    """
    if raw is None or raw.strip() == "":
        return generate_request_id(settings), None
    if validate_request_id(raw, settings) is not None:
        return raw, None
    return None, InvalidRequestId(reason=invalid_reason(raw, settings), raw=raw)


def set_request_id(value: str) -> Token[str]:
    """绑定到当前上下文，返回 reset 令牌。"""
    return _request_id_var.set(value)


def reset_request_id(token: Token[str]) -> None:
    """恢复上下文到 set 之前。"""
    _request_id_var.reset(token)


def get_request_id() -> str:
    """读取当前请求标识；上下文外为空串。"""
    return _request_id_var.get()
