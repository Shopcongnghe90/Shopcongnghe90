"""Fixtures dùng chung (SHARED — chỉ Phase 0/integrator sửa).

- ``pg_cluster`` (session): cluster PostgreSQL 16 tạm (initdb/pg_ctl; chạy bằng ``runuser -u postgres``
  khi shell là root), cổng ngẫu nhiên, thư mục tạm, dọn dẹp khi xong. Thiếu binary => skip lịch sự.
- ``pg_dsn`` (function): database mới tinh trong cluster đó (đã CREATE EXTENSION vector nếu có), drop sau test.
- ``temporal_env`` (session): WorkflowEnvironment.start_local với CLI có sẵn (ZEUS_TEMPORAL_CLI).
- Marker ``live``: skip trừ khi ZEUS_RUN_LIVE=1.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent
PG_BIN_CANDIDATES = [os.environ.get("ZEUS_PG_BIN", ""), "/usr/lib/postgresql/16/bin"]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("ZEUS_RUN_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(reason="live test: đặt ZEUS_RUN_LIVE=1 để chạy")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


# ------------------------------------------------------------------ PostgreSQL


def _pg_bin() -> Path | None:
    for c in PG_BIN_CANDIDATES:
        if c and (Path(c) / "initdb").exists() and (Path(c) / "pg_ctl").exists():
            return Path(c)
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _as_pg_user(cmd: list[str]) -> list[str]:
    if os.geteuid() == 0:
        return ["runuser", "-u", "postgres", "--", *cmd]
    return cmd


class PgCluster:
    def __init__(self, base_dir: Path, port: int) -> None:
        self.base_dir = base_dir
        self.port = port

    def dsn(self, dbname: str = "postgres") -> str:
        return f"host=127.0.0.1 port={self.port} user=postgres dbname={dbname}"


@pytest.fixture(scope="session")
def pg_cluster() -> Iterator[PgCluster]:
    bindir = _pg_bin()
    if bindir is None:
        pytest.skip("PostgreSQL 16 binaries không có (đặt ZEUS_PG_BIN)")
    if os.geteuid() == 0 and shutil.which("runuser") is None:
        pytest.skip("cần runuser để chạy postgres khi shell là root")
    try:
        import psycopg  # noqa: F401
    except ImportError:
        pytest.skip("psycopg chưa cài")

    base = Path(tempfile.mkdtemp(prefix="zeus-pg-"))
    data = base / "data"
    if os.geteuid() == 0:
        shutil.chown(base, user="postgres", group="postgres")
    port = _free_port()
    started = False
    try:
        subprocess.run(
            _as_pg_user([str(bindir / "initdb"), "-D", str(data), "-U", "postgres", "-A", "trust", "-E", "UTF8", "--no-sync"]),
            check=True,
            capture_output=True,
        )
        opts = f"-p {port} -k {base} -c listen_addresses=127.0.0.1 -c fsync=off -c full_page_writes=off"
        subprocess.run(
            _as_pg_user([str(bindir / "pg_ctl"), "-D", str(data), "-o", opts, "-l", str(base / "pg.log"), "-w", "-t", "60", "start"]),
            check=True,
            capture_output=True,
        )
        started = True
        yield PgCluster(base, port)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - môi trường hỏng
        log = (base / "pg.log").read_text() if (base / "pg.log").exists() else ""
        pytest.fail(f"không dựng được cluster Postgres: {exc.stderr!r}\n{log}")
    finally:
        if started:
            subprocess.run(_as_pg_user([str(bindir / "pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"]), capture_output=True)
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture()
def pg_dsn(pg_cluster: PgCluster) -> Iterator[str]:
    import psycopg

    name = f"zeus_t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_cluster.dsn(), autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_cluster.dsn(name)
    with psycopg.connect(dsn, autocommit=True) as conn:
        avail = conn.execute("SELECT 1 FROM pg_available_extensions WHERE name='vector'").fetchone()
        if avail:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    try:
        yield dsn
    finally:
        with psycopg.connect(pg_cluster.dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# ------------------------------------------------------------------ Temporal


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def temporal_env() -> AsyncIterator[object]:
    cli = os.environ.get("ZEUS_TEMPORAL_CLI", "/opt/zeus/bin/temporal")
    if not Path(cli).exists():
        pytest.skip(f"Temporal CLI không có tại {cli} (đặt ZEUS_TEMPORAL_CLI)")
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.testing import WorkflowEnvironment

    env = await WorkflowEnvironment.start_local(
        dev_server_existing_path=cli,
        data_converter=pydantic_data_converter,
        ui=False,
    )
    try:
        yield env
    finally:
        await env.shutdown()
