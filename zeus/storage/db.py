"""Kết nối PostgreSQL (psycopg 3). DSN lấy từ tham số hoặc ZEUS_DB_DSN."""

from __future__ import annotations

import os

import psycopg
from psycopg.rows import dict_row


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


async def aconnect(dsn: str | None = None, *, autocommit: bool = False, connect_timeout: int | None = None) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(
        _dsn(dsn), autocommit=autocommit, row_factory=dict_row, connect_timeout=connect_timeout or connect_timeout_s()
    )
