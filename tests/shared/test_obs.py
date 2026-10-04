from __future__ import annotations

import asyncio
import io
import json
import logging

import pytest

from zeus.contracts.models import TraceContext
from zeus.obs import (
    InMemorySpanExporter,
    SpanKind,
    SpanRecorder,
    SpanStatus,
    bind_trace,
    configure_logging,
    current_trace,
    new_span_id,
    new_trace_id,
    parse_traceparent,
    to_traceparent,
)
from zeus.obs.spans import ATTR_TASK, ATTR_TENANT


def test_ids_are_otel_shaped():
    t, s = new_trace_id(), new_span_id()
    assert len(t) == 32 and len(s) == 16
    int(t, 16), int(s, 16)
    assert new_trace_id() != t


def test_bind_trace_nesting_and_reset():
    assert current_trace() is None
    with bind_trace(tenant_id="zeusvn", task_id="t1") as outer:
        assert current_trace() == outer
        with bind_trace(workflow_id="wf1") as inner:
            assert inner.trace_id == outer.trace_id
            assert inner.task_id == "t1" and inner.workflow_id == "wf1"
        assert current_trace().workflow_id is None
    assert current_trace() is None


def test_traceparent_roundtrip():
    ctx = TraceContext(trace_id=new_trace_id(), span_id=new_span_id())
    parsed = parse_traceparent(to_traceparent(ctx))
    assert parsed.trace_id == ctx.trace_id and parsed.span_id == ctx.span_id
    assert parse_traceparent("garbage") is None
    assert parse_traceparent("00-" + "0" * 32 + "-" + "1" * 16 + "-01") is None


def test_json_logging_includes_trace_fields():
    buf = io.StringIO()
    handler = configure_logging("INFO", stream=buf)
    try:
        log = logging.getLogger("zeus.test")
        with bind_trace(tenant_id="khach-01", task_id="tsk_1", workflow_id="wf_1") as ctx:
            log.info("xin chào", extra={"component": "test"})
        line = json.loads(buf.getvalue().strip().splitlines()[-1])
        assert line["msg"] == "xin chào"
        assert line["trace_id"] == ctx.trace_id
        assert line["task_id"] == "tsk_1" and line["workflow_id"] == "wf_1" and line["tenant_id"] == "khach-01"
        assert line["component"] == "test"
    finally:
        logging.getLogger().removeHandler(handler)


def test_span_recorder_parenting_and_errors():
    exp = InMemorySpanExporter()
    rec = SpanRecorder([exp])
    with bind_trace(tenant_id="zeusvn", task_id="tsk_9"):
        with rec.span("outer", SpanKind.SERVER) as outer:
            with rec.span("model.call", SpanKind.CLIENT, **{"gen_ai.system": "anthropic"}) as inner:
                inner.set_attribute("gen_ai.usage.input_tokens", 10)
        with pytest.raises(RuntimeError):
            with rec.span("boom"):
                raise RuntimeError("x")
    by = {s.name: s for s in exp.spans}
    assert by["model.call"].parent_span_id == by["outer"].span_id
    assert by["model.call"].trace_id == by["outer"].trace_id
    assert by["outer"].status_code is SpanStatus.OK
    assert by["boom"].status_code is SpanStatus.ERROR and by["boom"].events[0].name == "exception"
    assert by["outer"].attributes[ATTR_TENANT] == "zeusvn" and by["outer"].attributes[ATTR_TASK] == "tsk_9"
    assert by["outer"].duration_ms is not None and by["outer"].duration_ms >= 0


async def test_trace_context_isolated_between_asyncio_tasks():
    seen = {}

    async def worker(name: str) -> None:
        with bind_trace(task_id=name):
            await asyncio.sleep(0.01)
            seen[name] = current_trace().task_id

    await asyncio.gather(worker("a"), worker("b"))
    assert seen == {"a": "a", "b": "b"}
