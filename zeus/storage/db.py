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


def connect(dsn: str | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(_dsn(dsn), autocommit=autocommit, row_factory=dict_row)


async def aconnect(dsn: str | None = None, *, autocommit: bool = False) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(_dsn(dsn), autocommit=autocommit, row_factory=dict_row)
