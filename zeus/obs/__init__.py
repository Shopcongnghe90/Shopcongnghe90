"""Observability dùng chung: trace context (contextvars), JSON logging, span recorder OTel-compatible."""

from zeus.obs.context import (
    bind_trace,
    current_trace,
    new_span_id,
    new_trace_id,
    parse_traceparent,
    to_traceparent,
)
from zeus.obs.logging import JsonFormatter, configure_logging, get_logger
from zeus.obs.spans import InMemorySpanExporter, Span, SpanExporter, SpanKind, SpanRecorder, SpanStatus

__all__ = [
    "bind_trace",
    "current_trace",
    "new_span_id",
    "new_trace_id",
    "parse_traceparent",
    "to_traceparent",
    "JsonFormatter",
    "configure_logging",
    "get_logger",
    "InMemorySpanExporter",
    "Span",
    "SpanExporter",
    "SpanKind",
    "SpanRecorder",
    "SpanStatus",
]
