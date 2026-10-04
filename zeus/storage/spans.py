"""Exporter ghi Span vào bảng trace_spans (migrations/000_core.sql)."""

from __future__ import annotations

import json
import logging
import queue
import threading

from zeus.obs.spans import ATTR_TASK, ATTR_TENANT, ATTR_WORKFLOW, Span
from zeus.storage.db import connect


log = logging.getLogger("zeus.storage.spans")


class PgSpanExporter:
    """Ghi span vào Postgres. ``background=True`` (production, trong event loop): ``export`` chỉ đẩy vào hàng đợi có giới hạn,
    một thread nền ghi DB => Postgres treo không đóng băng event loop của control worker (R8). Hàng đầy => bỏ span (best effort).
    ``background=False``: ghi đồng bộ (test/script). ``flush()`` chờ hàng đợi rỗng."""

    def __init__(self, dsn: str, *, background: bool = False, max_queue: int = 10_000, connect_timeout: int = 5) -> None:
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.dropped = 0
        self._q: queue.Queue[list[Span]] | None = None
        if background:
            self._q = queue.Queue(maxsize=max_queue)
            threading.Thread(target=self._loop, name="pg-span-exporter", daemon=True).start()

    def export(self, spans: list[Span]) -> None:
        if not spans:
            return
        if self._q is None:
            self._write(spans)
            return
        try:
            self._q.put_nowait(list(spans))
        except queue.Full:
            self.dropped += len(spans)

    def flush(self, timeout: float = 10.0) -> bool:
        """Chờ các span đã xếp hàng được ghi xong. True nếu hàng đợi đã rỗng trước hạn."""
        if self._q is None:
            return True
        done = threading.Event()

        def waiter() -> None:
            assert self._q is not None
            self._q.join()
            done.set()

        threading.Thread(target=waiter, daemon=True).start()
        return done.wait(timeout)

    def _loop(self) -> None:
        assert self._q is not None
        while True:
            batch = self._q.get()
            try:
                self._write(batch)
            except Exception:  # noqa: BLE001 - thread nền không được chết vì lỗi DB
                log.warning("ghi span lỗi (bỏ %d span)", len(batch), exc_info=True)
            finally:
                self._q.task_done()

    def _write(self, spans: list[Span]) -> None:
        with connect(self.dsn, connect_timeout=self.connect_timeout) as conn:
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
