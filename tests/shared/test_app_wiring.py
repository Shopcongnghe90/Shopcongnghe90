"""Nối app (integrator): mọi router A/B/C/D được mount, xác thực đúng chỗ, CLI quản trị (PostgreSQL thật, không Temporal)."""

from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest

from zeus.api.router import NullWorkflowControl
from zeus.app.admin import main as admin_main
from zeus.app.main import create_app, create_server_app
from zeus.app.system import build_system
from zeus.config import Settings
from zeus.contracts.api import HEADER_TENANT, HEADER_WORKER_TOKEN, Paths
from zeus.storage.migrate import apply_migrations

pytestmark = [pytest.mark.pg]
REPO = Path(__file__).resolve().parents[2]


def settings_for(dsn: str, tmp_path: Path, env: str = "test") -> Settings:
    return Settings(
        env=env, db_dsn=dsn, data_dir=tmp_path, models_config=REPO / "config/models.yaml", policy_config=REPO / "config/policy.yaml",
        brain_config=REPO / "config/brain.yaml", workers_config=REPO / "config/workers.yaml", channels_config=REPO / "config/channels.yaml",
    )


@pytest.fixture()
def dsn(pg_dsn: str) -> str:
    apply_migrations(pg_dsn, REPO / "migrations")
    return pg_dsn


async def test_all_routers_mounted_with_auth(dsn: str, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ZEUS_API_TOKEN", "tok-" + "x" * 30)
    system = build_system(settings_for(dsn, tmp_path), env={})
    assert isinstance(system.control.workflows, NullWorkflowControl) and system.deps.retriever is not None
    app = create_app(system=system)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://zeus") as c:
        h = {HEADER_TENANT: "zeusvn"}
        assert (await c.get(Paths.HEALTHZ)).json()["system"] is True
        assert (await c.get(Paths.READYZ)).json() == {"ready": True}
        assert (await c.get(Paths.TASKS, headers=h)).status_code == 401
        assert (await c.get(Paths.TASKS, headers={**h, "Authorization": "Bearer sai"})).status_code == 401
        auth = {**h, "Authorization": "Bearer tok-" + "x" * 30}
        assert (await c.get(Paths.TASKS, headers=auth)).json()["items"] == []
        info = (await c.get("/internal/system", headers=auth)).json()
        assert "test.run" in info["actions"] and info["channels"] == []  # không có secret kênh => không bật kênh nào
        assert (await c.get("/internal/evidence/task/tsk_x", params={"tenant_id": "zeusvn"}, headers=auth)).json() == []
        assert (await c.get("/internal/evidence/task/tsk_x", params={"tenant_id": "zeusvn"})).status_code == 401
        # Worker API: token theo worker; Workbench: đăng nhập; hooks: kênh chưa bật => 404
        assert (await c.post(Paths.WORKER_POLL, json={"worker_id": "w1"})).status_code == 401
        assert (await c.post(Paths.WORKER_POLL, json={"worker_id": "w1"}, headers={HEADER_WORKER_TOKEN: "zwt_sai"})).status_code == 401
        wb = await c.get("/wb")
        assert wb.status_code == 303 and wb.headers["location"] == "/wb/login"
        assert (await c.post(Paths.HOOK_ZALO_BOT, content=b"{}")).status_code == 404
        # Ingest không Temporal => NullWorkflowControl ghi lại lời gọi (đường production dùng TemporalWorkflowControl)
        ev = {"event": {"tenant_id": "zeusvn", "channel": "internal", "kind": "command", "text": "kiểm tra api backend", "external_id": "w-1"}}
        r = await c.post(Paths.EVENTS, json=ev, headers=auth)
        assert r.status_code == 200 and system.control.workflows.started == [r.json()["task_id"]]


async def test_prod_without_api_token_fails_closed(dsn: str, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ZEUS_API_TOKEN", raising=False)
    app = create_app(system=build_system(settings_for(dsn, tmp_path, env="prod"), env={}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://zeus") as c:
        assert (await c.get(Paths.TASKS, headers={HEADER_TENANT: "zeusvn"})).status_code == 503


async def test_server_app_factory_mounts_routes_without_connecting() -> None:
    """Chưa chạy lifespan (chưa kết nối Temporal/PG): route đã có (503 = chưa cấu hình, không phải 404)."""
    app = create_server_app(Settings(env="test"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://zeus") as c:
        assert (await c.get(Paths.TASKS)).status_code == 503
        assert (await c.get("/wb/login")).status_code == 503
        assert (await c.post(Paths.HOOK_ZALO_BOT, content=b"{}")).status_code == 503
        assert (await c.get(Paths.READYZ)).status_code == 503
        assert (await c.get("/khong-co")).status_code == 404


def test_admin_cli_issues_worker_token(dsn: str, tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("ZEUS_DB_DSN", dsn)
    assert admin_main(["issue-worker-token", "w-cli"]) == 0
    token = capsys.readouterr().out.strip()
    assert token.startswith("zwt_")
    from zeus.workers.registry import PgWorkerRegistry

    import asyncio

    grant = asyncio.run(PgWorkerRegistry(dsn).verify_token(token))
    assert grant is not None and grant.worker_id == "w-cli"
    monkeypatch.setattr("sys.stdin", io.StringIO("mot-mat-khau-du-dai\n"))
    assert admin_main(["hash-password"]) == 0
    assert capsys.readouterr().out.startswith("pbkdf2_sha256$")


async def test_test_run_executor_requires_configured_suite(tmp_path: Path) -> None:
    """test.run chỉ được quảng bá khi worker có test_repo; không nhận tham số tự do (goal) từ control."""
    from zeus.contracts.models import TypedAction
    from zeus_worker.agent import WorkerAgent
    from zeus_worker.config import WorkerConfig
    from zeus_worker.executors import ExecContext, ExecError, ExecutorRegistry

    bare = WorkerConfig(server_url="http://x", worker_id="w", work_dir=tmp_path)
    caps = {c.name for c in WorkerAgent(bare, client=None).build_info().capabilities}  # type: ignore[arg-type]
    assert "action:test.run" not in caps and "action:noop.echo" in caps
    with pytest.raises(ExecError):
        await ExecutorRegistry().get("test.run").run(ExecContext(TypedAction(name="test.run", args={"goal": "x"}), bare))  # type: ignore[union-attr]

    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    cfg = WorkerConfig(server_url="http://x", worker_id="w", work_dir=tmp_path, repo_roots=[tmp_path], test_repo=repo, test_target="tests")
    assert "action:test.run" in {c.name for c in WorkerAgent(cfg, client=None).build_info().capabilities}  # type: ignore[arg-type]
    out = await ExecutorRegistry().get("test.run").run(ExecContext(TypedAction(name="test.run", args={"goal": "; rm -rf /"}), cfg))  # type: ignore[union-attr]
    assert out.ok and out.evidence[0].passed is True and out.output["test_run"]["passed"] == 1
    outside = WorkerConfig(server_url="http://x", worker_id="w", work_dir=tmp_path, repo_roots=[tmp_path / "khac"], test_repo=repo)
    with pytest.raises(ExecError):
        await ExecutorRegistry().get("test.run").run(ExecContext(TypedAction(name="test.run"), outside))  # type: ignore[union-attr]
