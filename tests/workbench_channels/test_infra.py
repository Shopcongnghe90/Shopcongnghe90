"""Migration 401 + PgDedupe (Postgres thật), dedupe bộ nhớ, và test `live` (mặc định skip)."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from zeus.channels.base import MemoryDedupe
from zeus.channels.pg_dedupe import PgDedupe
from zeus.storage import apply_migrations, discover_migrations

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def test_migration_401_is_owned_by_D():
    m = {x.version: x for x in discover_migrations(MIGRATIONS)}
    assert m[401].owner == "D"
    sql = m[401].path.read_text(encoding="utf-8")
    assert "secret" not in sql.lower().replace("không phải secret", "").replace("không chứa secret", "")


async def test_memory_dedupe_ttl_and_release():
    t = [1000.0]
    d = MemoryDedupe(ttl_s=10, clock=lambda: t[0])
    assert await d.claim("a") and not await d.claim("a")
    await d.release("a")
    assert await d.claim("a")
    t[0] += 11
    assert await d.claim("a")  # hết TTL


@pytest.mark.pg
async def test_pg_dedupe_on_real_postgres(pg_dsn: str):
    apply_migrations(pg_dsn, MIGRATIONS)
    d = PgDedupe(pg_dsn, ttl_s=3600)
    assert await d.claim("zalo_bot:c1:m1") is True
    assert await d.claim("zalo_bot:c1:m1") is False
    assert await d.claim("zalo_bot:c1:m2") is True
    await d.release("zalo_bot:c1:m1")
    assert await d.claim("zalo_bot:c1:m1") is True
    with psycopg.connect(pg_dsn) as conn:
        conn.execute("UPDATE channel_dedupe SET seen_at = now() - interval '2 hours'")
        conn.commit()
    assert await d.claim("zalo_bot:c1:m1") is True  # bản cũ quá TTL bị dọn
    tables = {r[0] for r in psycopg.connect(pg_dsn).execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'").fetchall()}
    assert {"channel_accounts", "channel_cursors", "channel_dedupe"} <= tables


@pytest.mark.live
async def test_live_odoo_staging_smoke():  # chỉ chạy khi ZEUS_RUN_LIVE=1 và có ERP staging thật
    import os

    from zeus.integrations.odoo import OdooAccess, OdooJson2Client

    c = OdooJson2Client(os.environ["ZEUS_LIVE_ODOO_URL"], os.environ["ZEUS_LIVE_ODOO_DB"], lambda: os.environ.get("ZEUS_LIVE_ODOO_KEY"), OdooAccess(frozenset({"res.partner"})))
    assert isinstance(await c.call("res.partner", "search_count", params={"domain": []}), int)
