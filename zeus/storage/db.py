"""Kết nối PostgreSQL (psycopg 3). DSN lấy từ tham số hoặc ZEUS_DB_DSN."""

from __future__ import annotations

import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

from zeus.storage.pool import AsyncPool, PoolClosed, PoolTimeout  # noqa: F401


def _dsn(dsn: str | None) -> str:
    value = dsn or os.environ.get("ZEUS_DB_DSN")
    if not value:
        raise RuntimeError("ZEUS_DB_DSN chưa được đặt")
    return value


def connect_timeout_s() -> int:
    """Trần thời gian thiết lập kết nối (giây). Postgres treo/firewall drop không được khoá tiến trình vô hạn (R8)."""
    try:
        return max(1, int(os.environ.get("ZEUS_DB_CONNECT_TIMEOUT", "10")))
    except ValueError:
        return 10


def connect(dsn: str | None = None, *, autocommit: bool = False, connect_timeout: int | None = None) -> psycopg.Connection:
    return psycopg.connect(_dsn(dsn), autocommit=autocommit, row_factory=dict_row, connect_timeout=connect_timeout or connect_timeout_s())


_POOLS: dict[str, AsyncPool] = {}


def register_pool(pool: AsyncPool) -> None:
    """Từ lúc này ``aconnect`` cùng DSN và cùng event loop lấy kết nối từ pool (store không cần đổi code)."""
    _POOLS[pool.dsn] = pool


def unregister_pool(pool: AsyncPool) -> None:
    if _POOLS.get(pool.dsn) is pool:
        del _POOLS[pool.dsn]


def get_pool(dsn: str) -> AsyncPool | None:
    return _POOLS.get(dsn)


async def open_pool(dsn: str, *, max_size: int = 10, **kw: Any) -> AsyncPool:
    """Mở pool cho DSN trong event loop hiện tại và đăng ký. Đã có pool dùng được ở loop này thì trả lại pool đó (idempotent)."""
    cur = _POOLS.get(dsn)
    if cur is not None and cur.usable_here():
        return cur
    pool = await AsyncPool(dsn, max_size=max_size, **kw).open()
    register_pool(pool)
    return pool


async def close_pool(pool: AsyncPool) -> None:
    unregister_pool(pool)
    await pool.close()


async def aconnect(dsn: str | None = None, *, autocommit: bool = False, connect_timeout: int | None = None) -> psycopg.AsyncConnection:
    """Kết nối async. Có pool đăng ký cho DSN và đang ở đúng loop => lease từ pool (``async with`` dùng như kết nối thường);
    ngược lại mở một kết nối riêng như trước."""
    d = _dsn(dsn)
    pool = _POOLS.get(d)
    if pool is not None and pool.usable_here():
        return await pool.acquire(autocommit=autocommit)  # type: ignore[return-value]
    return await psycopg.AsyncConnection.connect(d, autocommit=autocommit, row_factory=dict_row, connect_timeout=connect_timeout or connect_timeout_s())
