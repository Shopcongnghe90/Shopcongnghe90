"""Dedupe bền vững trên PostgreSQL (bảng channel_dedupe, migration 401). psycopg đồng bộ chạy trong thread."""

from __future__ import annotations

import asyncio

import psycopg


class PgDedupe:
    def __init__(self, dsn: str, ttl_s: int = 86400, inflight_s: int = 120) -> None:
        self._dsn, self._ttl, self._inflight = dsn, ttl_s, inflight_s

    # Hai pha: claim = "đang xử lý" (hết hạn sau inflight_s nếu tiến trình chết giữa chừng, nền tảng retry được xử lý lại);
    # confirm = "đã ingest xong" (khoá ``done:<key>`` sống ttl_s). Tránh giữ khoá 24h cho event chưa từng được lưu (R3).
    def _claim(self, key: str) -> bool:
        with psycopg.connect(self._dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute("DELETE FROM channel_dedupe WHERE seen_at < now() - make_interval(secs => %s)", (self._ttl,))
            if conn.execute("SELECT 1 FROM channel_dedupe WHERE dedupe_key = %s", ("done:" + key,)).fetchone():
                return False
            cur = conn.execute("INSERT INTO channel_dedupe (dedupe_key) VALUES (%s) ON CONFLICT DO NOTHING", (key,))
            if cur.rowcount == 1:
                return True
            cur = conn.execute(  # claim cũ chưa confirm và đã quá hạn xử lý: chiếm lại
                "UPDATE channel_dedupe SET seen_at = now() WHERE dedupe_key = %s AND seen_at < now() - make_interval(secs => %s)",
                (key, self._inflight),
            )
            return cur.rowcount == 1

    def _confirm(self, key: str) -> None:
        with psycopg.connect(self._dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute("INSERT INTO channel_dedupe (dedupe_key) VALUES (%s) ON CONFLICT DO NOTHING", ("done:" + key,))

    def _release(self, key: str) -> None:
        with psycopg.connect(self._dsn, autocommit=True, connect_timeout=10) as conn:
            conn.execute("DELETE FROM channel_dedupe WHERE dedupe_key = %s", (key,))

    async def claim(self, key: str) -> bool:
        return await asyncio.to_thread(self._claim, key)

    async def release(self, key: str) -> None:
        await asyncio.to_thread(self._release, key)

    async def confirm(self, key: str) -> None:
        await asyncio.to_thread(self._confirm, key)
