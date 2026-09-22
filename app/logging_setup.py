"""结构化日志：每条日志自动附带当前请求标识。

- 日志行采用 JSON 结构，包含 timestamp/level/logger/message/request_id；
- 诊断信息（含内部异常）只写日志，响应体不携带；
- 未绑定请求（如启动阶段）时 request_id 为空串。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from .request_id import get_request_id

#: 诊断信息（含内部堆栈/依赖细节）专用 logger
diagnostic_logger = logging.getLogger("observability.diagnostic")

#: 每请求一条的访问记录 logger
access_logger = logging.getLogger("observability.access")

#: 告警事件 logger
alert_logger = logging.getLogger("observability.alert")


class RequestIdFilter(logging.Filter):
    """为日志记录注入 request_id 字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


class JsonFormatter(logging.Formatter):
    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "request_id",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", ""),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    """幂等配置根 logger：JSON 格式到 stdout + 请求标识过滤器。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    _CONFIGURED = True
