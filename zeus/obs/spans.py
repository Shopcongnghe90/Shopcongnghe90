"""Span recorder tương thích OpenTelemetry (trường theo data model OTel + semantic conventions gen_ai.*).

Không bắt buộc cài opentelemetry: exporter mặc định là in-memory / JSON log / Postgres (zeus.storage.spans).
``OTelBridgeExporter`` chỉ hoạt động khi đã cài opentelemetry-sdk (extra ``otel``).
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Iterator
from enum import Enum
from typing import Any, Protocol

from pydantic import Field

from zeus.contracts.models import DEFAULT_TENANT, TraceContext, ZeusModel
from zeus.obs.context import _CURRENT, new_span_id, new_trace_id

# Semantic convention keys (OTel) dùng thống nhất
ATTR_TENANT = "zeus.tenant_id"
ATTR_TASK = "zeus.task_id"
ATTR_WORKFLOW = "zeus.workflow_id"
ATTR_GENAI_SYSTEM = "gen_ai.system"
ATTR_GENAI_REQUEST_MODEL = "gen_ai.request.model"
ATTR_GENAI_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_GENAI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
ATTR_COST_USD = "zeus.cost_usd"


class SpanKind(str, Enum):
    INTERNAL = "INTERNAL"
    SERVER = "SERVER"
    CLIENT = "CLIENT"
    PRODUCER = "PRODUCER"
    CONSUMER = "CONSUMER"


class SpanStatus(str, Enum):
    UNSET = "UNSET"
    OK = "OK"
    ERROR = "ERROR"


class SpanEvent(ZeusModel):
    name: str
    time_unix_nano: int
    attributes: dict[str, Any] = Field(default_factory=dict)


class Span(ZeusModel):
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    name: str
    kind: SpanKind = SpanKind.INTERNAL
    start_time_unix_nano: int
    end_time_unix_nano: int | None = None
    status_code: SpanStatus = SpanStatus.UNSET
    status_message: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    events: list[SpanEvent] = Field(default_factory=list)
    resource: dict[str, Any] = Field(default_factory=dict)

    @property
    def duration_ms(self) -> float | None:
        if self.end_time_unix_nano is None:
            return None
        return (self.end_time_unix_nano - self.start_time_unix_nano) / 1e6

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, **attributes: Any) -> None:
        self.events.append(SpanEvent(name=name, time_unix_nano=time.time_ns(), attributes=attributes))


class SpanExporter(Protocol):
    def export(self, spans: list[Span]) -> None: ...


class InMemorySpanExporter:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    def export(self, spans: list[Span]) -> None:
        self.spans.extend(spans)

    def clear(self) -> None:
        self.spans.clear()


class LogSpanExporter:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("zeus.spans")

    def export(self, spans: list[Span]) -> None:
        for s in spans:
            self.logger.info("span", extra={"span": s.model_dump(mode="json")})


class OTelBridgeExporter:  # pragma: no cover - chỉ chạy khi có opentelemetry-sdk
    """Chuyển Span sang OTel SDK tracer. Import lười; thiếu thư viện => ImportError rõ ràng."""

    def __init__(self, tracer_name: str = "zeus") -> None:
        try:
            from opentelemetry import trace  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError("opentelemetry-sdk chưa cài; cài extra 'otel' để dùng OTelBridgeExporter") from exc
        self._tracer = trace.get_tracer(tracer_name)

    def export(self, spans: list[Span]) -> None:
        for s in spans:
            otel_span = self._tracer.start_span(s.name, start_time=s.start_time_unix_nano, attributes=s.attributes)
            otel_span.end(end_time=s.end_time_unix_nano)


class SpanRecorder:
    """Ghi span lồng nhau dựa trên trace context hiện tại; dùng được trong code sync lẫn async."""

    def __init__(self, exporters: list[SpanExporter] | None = None, resource: dict[str, Any] | None = None) -> None:
        self.exporters: list[SpanExporter] = list(exporters or [])
        self.resource = resource or {"service.name": "zeus"}

    @contextlib.contextmanager
    def span(self, name: str, kind: SpanKind = SpanKind.INTERNAL, **attributes: Any) -> Iterator[Span]:
        parent = _CURRENT.get()
        trace_id = parent.trace_id if parent else new_trace_id()
        span = Span(
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=parent.span_id if parent else None,
            name=name,
            kind=kind,
            start_time_unix_nano=time.time_ns(),
            attributes=dict(attributes),
            resource=dict(self.resource),
        )
        if parent:
            span.attributes.setdefault(ATTR_TENANT, parent.tenant_id)
            if parent.task_id:
                span.attributes.setdefault(ATTR_TASK, parent.task_id)
            if parent.workflow_id:
                span.attributes.setdefault(ATTR_WORKFLOW, parent.workflow_id)
        child_ctx = (parent or TraceContext(trace_id=trace_id, tenant_id=DEFAULT_TENANT)).model_copy(
            update={"span_id": span.span_id, "trace_id": trace_id}
        )
        token = _CURRENT.set(child_ctx)
        try:
            yield span
            if span.status_code is SpanStatus.UNSET:
                span.status_code = SpanStatus.OK
        except BaseException as exc:
            span.status_code = SpanStatus.ERROR
            span.status_message = f"{type(exc).__name__}: {exc}"
            span.add_event("exception", **{"exception.type": type(exc).__name__, "exception.message": str(exc)})
            raise
        finally:
            _CURRENT.reset(token)
            span.end_time_unix_nano = time.time_ns()
            for exp in self.exporters:
                try:
                    exp.export([span])
                except Exception:  # exporter lỗi không được làm hỏng nghiệp vụ
                    logging.getLogger("zeus.obs").exception("span exporter failed")
