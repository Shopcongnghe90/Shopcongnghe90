"""Trace context lan truyền bằng contextvars (an toàn với asyncio: mỗi task có bản sao riêng)."""

from __future__ import annotations

import contextlib
import contextvars
import re
import secrets
from collections.abc import Iterator

from zeus.contracts.models import DEFAULT_TENANT, TraceContext

_CURRENT: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar("zeus_trace", default=None)

_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def new_trace_id() -> str:
    """32 hex (128-bit), khác 0 — theo W3C/OTel."""
    while True:
        tid = secrets.token_hex(16)
        if tid != "0" * 32:
            return tid


def new_span_id() -> str:
    while True:
        sid = secrets.token_hex(8)
        if sid != "0" * 16:
            return sid


def current_trace() -> TraceContext | None:
    return _CURRENT.get()


@contextlib.contextmanager
def bind_trace(
    ctx: TraceContext | None = None,
    *,
    tenant_id: str | None = None,
    task_id: str | None = None,
    workflow_id: str | None = None,
    span_id: str | None = None,
) -> Iterator[TraceContext]:
    """Gắn TraceContext vào ngữ cảnh hiện tại. Không truyền ctx => kế thừa ctx hiện có (hoặc tạo mới)
    và ghi đè các trường được chỉ định."""
    base = ctx or _CURRENT.get() or TraceContext(trace_id=new_trace_id(), tenant_id=tenant_id or DEFAULT_TENANT)
    updates = {
        k: v
        for k, v in {"tenant_id": tenant_id, "task_id": task_id, "workflow_id": workflow_id, "span_id": span_id}.items()
        if v is not None
    }
    bound = base.model_copy(update=updates) if updates else base
    token = _CURRENT.set(bound)
    try:
        yield bound
    finally:
        _CURRENT.reset(token)


def to_traceparent(ctx: TraceContext) -> str:
    return f"00-{ctx.trace_id}-{ctx.span_id or new_span_id()}-01"


def parse_traceparent(value: str | None, *, tenant_id: str = DEFAULT_TENANT) -> TraceContext | None:
    if not value:
        return None
    m = _TRACEPARENT_RE.match(value.strip().lower())
    if not m or m.group(1) == "0" * 32 or m.group(2) == "0" * 16:
        return None
    return TraceContext(trace_id=m.group(1), span_id=m.group(2), tenant_id=tenant_id)
