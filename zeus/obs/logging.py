"""Structured JSON logging: mỗi dòng log là 1 object JSON có trace_id/task_id/workflow_id/tenant_id."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any, TextIO

from zeus.obs.context import current_trace

_RESERVED = set(vars(logging.LogRecord("x", 0, "x", 0, "x", None, None))) | {"message", "asctime", "taskName"}


# Che bí mật nhúng trong URL/chuỗi (token bot Zalo nằm trong đường dẫn ``/bot<TOKEN>/``, access_token, Bearer...).
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!/)(/bot)[^/\s\"'?#]+(?=/)"), r"\1***"),
    (re.compile(r"(?i)\b(access_token|api_key|apikey|secret|password)(=|\"?\s*:\s*\"?)[^&\s\"',;]+"), r"\1\2***"),
    (re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]+"), r"\1 ***"),
)


def redact_text(text: str) -> str:
    for pat, repl in _REDACTIONS:
        text = pat.sub(repl, text)
    return text


# Thư viện HTTP ghi nguyên URL ở mức INFO; hạ xuống WARNING để không rò token (phòng thủ nhiều lớp cùng redact_text).
_QUIET_LOGGERS = ("httpx", "httpcore")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_text(record.getMessage()),
        }
        ctx = current_trace()
        out["trace_id"] = ctx.trace_id if ctx else None
        out["span_id"] = ctx.span_id if ctx else None
        out["task_id"] = ctx.task_id if ctx else None
        out["workflow_id"] = ctx.workflow_id if ctx else None
        out["tenant_id"] = ctx.tenant_id if ctx else None
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                out[key] = redact_text(value) if isinstance(value, str) else value
        if record.exc_info:
            out["exc"] = redact_text(self.formatException(record.exc_info))
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
    for name in _QUIET_LOGGERS:
        lg = logging.getLogger(name)
        if lg.level == logging.NOTSET or lg.level < logging.WARNING:
            lg.setLevel(logging.WARNING)
    return handler


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
