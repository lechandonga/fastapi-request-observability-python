"""结构化（JSON Lines）日志配置。

每条日志输出为单行 JSON，字段固定：``ts / level / logger / message /
request_id``，其中 ``request_id`` 自动取自当前请求的 ContextVar，
因此无需在每个打点处手动传递，也不会在并发请求间串扰。

使用 ``json.dumps`` 序列化，换行/引号均会被转义，非法标识中的换行符等
无法伪造日志行（防日志注入）。
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from .context import get_request_id


class JsonLineFormatter(logging.Formatter):
    """把 LogRecord 序列化为单行 JSON。"""

    # 只提取这些标准属性，避免把模块路径/异常对象等内部信息混入日志行。
    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": dt.datetime.fromtimestamp(
                record.created, tz=dt.timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": get_request_id(),
        }
        # 透传 logger.info(..., extra={...}) 中的诊断字段。
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            # 堆栈仅落在服务端日志，且与 request_id 同行，便于排查。
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["exc_message"] = str(record.exc_info[1])
            payload["stack"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    """为 ``app`` 命名空间挂载一次 JSON handler（幂等）。

    只配置 ``app`` logger 并关闭向上传播，避免 uvicorn / 第三方库日志
    被重复格式化；调用多次安全。
    """
    global _configured
    if _configured:
        return

    handler = logging.StreamHandler()
    handler.setFormatter(JsonLineFormatter())

    app_logger = logging.getLogger("app")
    app_logger.setLevel(level)
    app_logger.handlers.clear()
    app_logger.addHandler(handler)
    app_logger.propagate = False

    _configured = True
