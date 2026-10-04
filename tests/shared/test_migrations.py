from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from zeus.obs import SpanRecorder, bind_trace
from zeus.storage import MigrationError, apply_migrations, discover_migrations, migration_owner, migration_status
from zeus.storage.spans import PgSpanExporter

REPO_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def test_repo_migrations_follow_numbering_ownership():
    migs = discover_migrations(REPO_MIGRATIONS)
    assert migs[0].version == 0 and migs[0].owner == "shared"
    assert migration_owner(150) == "A" and migration_owner(299) == "B"
    assert migration_owner(301) == "C" and migration_owner(410) == "D"
    with pytest.raises(MigrationError):
        migration_owner(500)


def test_discover_rejects_bad_names(tmp_path: Path):
    (tmp_path / "001_ok.sql").write_text("SELECT 1;")
    (tmp_path / "1_bad.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError):
        discover_migrations(tmp_path)
    (tmp_path / "1_bad.sql").unlink()
    (tmp_path / "001_dup.sql").write_text("SELECT 2;")
    with pytest.raises(MigrationError):
        discover_migrations(tmp_path)


@pytest.mark.pg
def test_apply_core_idempotent_and_append_only(pg_dsn: str):
    applied = apply_migrations(pg_dsn, REPO_MIGRATIONS)
    assert [m.version for m in applied] == [m.version for m in discover_migrations(REPO_MIGRATIONS)]
    assert apply_migrations(pg_dsn, REPO_MIGRATIONS) == []
    assert all(r["state"] == "applied" for r in migration_status(pg_dsn, REPO_MIGRATIONS))
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        assert conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone() is not None
        assert conn.execute("SELECT tenant_id FROM tenants").fetchall() == [("zeusvn",)]
        conn.execute("INSERT INTO audit_log (tenant_id, actor, action, risk) VALUES ('zeusvn','system','test.write','R1')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE audit_log SET action='tampered'")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM audit_log")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("TRUNCATE audit_log")
        assert conn.execute("SELECT action FROM audit_log").fetchall() == [("test.write",)]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO tenants (tenant_id, display_name) VALUES ('Bad Tenant', 'x')")


@pytest.mark.pg
def test_checksum_mismatch_and_failed_migration_rolls_back(pg_dsn: str, tmp_path: Path):
    (tmp_path / "000_core.sql").write_text((REPO_MIGRATIONS / "000_core.sql").read_text())
    (tmp_path / "100_a_demo.sql").write_text("CREATE TABLE a_demo (id int primary key);")
    assert [m.version for m in apply_migrations(pg_dsn, tmp_path)] == [0, 100]
    (tmp_path / "100_a_demo.sql").write_text("CREATE TABLE a_demo (id bigint primary key);")
    with pytest.raises(MigrationError, match="checksum"):
        apply_migrations(pg_dsn, tmp_path)
    (tmp_path / "100_a_demo.sql").write_text("CREATE TABLE a_demo (id int primary key);")
    (tmp_path / "200_b_broken.sql").write_text("CREATE TABLE b_ok (id int); SELECT * FROM does_not_exist;")
    with pytest.raises(psycopg.Error):
        apply_migrations(pg_dsn, tmp_path)
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        assert conn.execute("SELECT to_regclass('b_ok')").fetchone() == (None,)
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone() == (100,)


@pytest.mark.pg
def test_pg_span_exporter(pg_dsn: str):
    apply_migrations(pg_dsn, REPO_MIGRATIONS)
    rec = SpanRecorder([PgSpanExporter(pg_dsn)])
    with bind_trace(tenant_id="zeusvn", task_id="tsk_pg"):
        with rec.span("outer"):
            with rec.span("inner"):
                pass
    with psycopg.connect(pg_dsn) as conn:
        rows = conn.execute("SELECT name, parent_span_id IS NULL, tenant_id, task_id, status_code FROM trace_spans ORDER BY name").fetchall()
    assert rows == [("inner", False, "zeusvn", "tsk_pg", "OK"), ("outer", True, "zeusvn", "tsk_pg", "OK")]
