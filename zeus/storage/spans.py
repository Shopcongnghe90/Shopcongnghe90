"""Exporter ghi Span vào bảng trace_spans (migrations/000_core.sql)."""

from __future__ import annotations

import json

from zeus.obs.spans import ATTR_TASK, ATTR_TENANT, ATTR_WORKFLOW, Span
from zeus.storage.db import connect


class PgSpanExporter:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def export(self, spans: list[Span]) -> None:
        if not spans:
            return
        with connect(self.dsn) as conn:
            with conn.cursor() as cur:
                for s in spans:
                    cur.execute(
                        """
                        INSERT INTO trace_spans (trace_id, span_id, parent_span_id, name, kind,
                            start_time_unix_nano, end_time_unix_nano, status_code, status_message,
                            tenant_id, task_id, workflow_id, attributes, events)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
                        ON CONFLICT (trace_id, span_id) DO NOTHING
                        """,
                        (
                            s.trace_id,
                            s.span_id,
                            s.parent_span_id,
                            s.name,
                            s.kind.value,
                            s.start_time_unix_nano,
                            s.end_time_unix_nano,
                            s.status_code.value,
                            s.status_message,
                            s.attributes.get(ATTR_TENANT),
                            s.attributes.get(ATTR_TASK),
                            s.attributes.get(ATTR_WORKFLOW),
                            json.dumps(s.attributes, default=str),
                            json.dumps([e.model_dump(mode="json") for e in s.events]),
                        ),
                    )
