"""Structured JSON logging: mỗi dòng log là 1 object JSON có trace_id/task_id/workflow_id/tenant_id."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, TextIO

from zeus.obs.context import current_trace

_RESERVED = set(vars(logging.LogRecord("x", 0, "x", 0, "x", None, None))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = current_trace()
        out["trace_id"] = ctx.trace_id if ctx else None
        out["span_id"] = ctx.span_id if ctx else None
        out["task_id"] = ctx.task_id if ctx else None
        out["workflow_id"] = ctx.workflow_id if ctx else None
        out["tenant_id"] = ctx.tenant_id if ctx else None
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


def configure_logging(level: str | int = "INFO", stream: TextIO | None = None) -> logging.Handler:
    """Cấu hình root logger xuất JSON. Idempotent: thay handler JSON cũ nếu có."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_zeus_json", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler._zeus_json = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level if isinstance(level, int) else level.upper())
    return handler


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
