"""Dedupe bền vững trên PostgreSQL (bảng channel_dedupe, migration 401). psycopg đồng bộ chạy trong thread."""

from __future__ import annotations

import asyncio

import psycopg


class PgDedupe:
    def __init__(self, dsn: str, ttl_s: int = 86400) -> None:
        self._dsn, self._ttl = dsn, ttl_s

    def _claim(self, key: str) -> bool:
        with psycopg.connect(self._dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM channel_dedupe WHERE seen_at < now() - make_interval(secs => %s)", (self._ttl,))
            cur = conn.execute("INSERT INTO channel_dedupe (dedupe_key) VALUES (%s) ON CONFLICT DO NOTHING", (key,))
            return cur.rowcount == 1

    def _release(self, key: str) -> None:
        with psycopg.connect(self._dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM channel_dedupe WHERE dedupe_key = %s", (key,))

    async def claim(self, key: str) -> bool:
        return await asyncio.to_thread(self._claim, key)

    async def release(self, key: str) -> None:
        await asyncio.to_thread(self._release, key)
