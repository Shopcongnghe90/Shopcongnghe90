"""Truy cập Postgres dùng chung cho B: mỗi thao tác mở 1 kết nối async (đơn giản, đủ cho Phase 1)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg

from zeus.storage.db import aconnect


class PgBase:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    @asynccontextmanager
    async def conn(self, *, autocommit: bool = True) -> AsyncIterator[psycopg.AsyncConnection]:
        c = await aconnect(self.dsn, autocommit=autocommit)
        try:
            yield c
        finally:
            await c.close()


def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"
