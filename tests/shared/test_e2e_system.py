"""E2E toàn hệ thống (integrator): PostgreSQL 16 thật + Temporal dev server thật + thin worker trong tiến trình.

Luồng: POST /api/v1/events (Control API, A) -> TaskWorkflow (Temporal, chạy bởi zeus.app.worker_main) -> planner/critic ->
dispatch: scheduler (C) chọn worker, AssignmentQueue PG -> thin worker (zeus_worker) long-poll /worker/v1, chạy typed action
``test.run`` (pytest thật) -> kết quả + log về Worker API -> async completion -> Judge -> EvidenceRecord + Outcome (B, PG)
-> Workbench /wb (D) hiển thị task/evidence. Cộng approval gate: bước R2 không chạy gì cho tới khi có ApprovalDecision.
Không có Claude Cloud, không key provider, không mạng ngoài (CLAUDE_CLOUD_AVAILABLE=false).
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from zeus.app.main import create_app
from zeus.app.system import System, build_system
from zeus.app.worker_main import run as run_control_worker
from zeus.config import Settings
from zeus.contracts.api import HEADER_TENANT, Paths
from zeus.contracts.models import Outcome, TaskStatus, Verdict, VerdictDecision
from zeus.storage.migrate import apply_migrations
from zeus.workbench.security import AuthConfig, hash_password
from zeus_worker.agent import WorkerAgent
from zeus_worker.client import WorkerApiClient
from zeus_worker.config import WorkerConfig

pytestmark = [pytest.mark.pg, pytest.mark.temporal, pytest.mark.cloud_exit_workflow, pytest.mark.cloud_exit_worker_control,
              pytest.mark.cloud_exit_evidence]

REPO = Path(__file__).resolve().parents[2]
TENANT = "zeusvn"
WB_PASSWORD = "e2e-mat-khau-dai-du"


@dataclass
class Stack:
    system: System
    app: Any
    http: httpx.AsyncClient
    dsn: str
    temporal: Any

    def q(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as c:
            return c.execute(sql, args).fetchall()

    async def ingest(self, text: str, external_id: str) -> dict[str, Any]:
        body = {"event": {"tenant_id": TENANT, "channel": "internal", "kind": "command", "text": text, "external_id": external_id}}
        r = await self.http.post(Paths.EVENTS, json=body, headers={HEADER_TENANT: TENANT})
        assert r.status_code == 200, r.text
        return r.json()

    async def verdict(self, task_id: str, timeout: float = 120.0):  # noqa: ANN201
        return await asyncio.wait_for(self.temporal.client.get_workflow_handle(task_id, result_type=Verdict).result(), timeout)

    async def wb_login(self) -> httpx.AsyncClient:
        wb = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://zeus")
        page = await wb.get("/wb/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
        r = await wb.post("/wb/login", data={"csrf_token": csrf, "username": "operator", "password": WB_PASSWORD})
        assert r.status_code == 303, r.text
        return wb


@pytest.fixture()
async def stack(pg_dsn: str, temporal_env, tmp_path: Path, monkeypatch) -> AsyncIterator[Stack]:
    monkeypatch.setenv("CLAUDE_CLOUD_AVAILABLE", "false")
    monkeypatch.delenv("ZEUS_API_TOKEN", raising=False)
    for k in [k for k in os.environ if k.startswith("CLAUDE_CODE_")]:
        monkeypatch.delenv(k)
    apply_migrations(pg_dsn, REPO / "migrations")
    settings = Settings(
        env="test", db_dsn=pg_dsn, claude_cloud_available=False, data_dir=tmp_path / "var",
        models_config=REPO / "config/models.yaml", policy_config=REPO / "config/policy.yaml", brain_config=REPO / "config/brain.yaml",
        workers_config=REPO / "config/workers.yaml", channels_config=REPO / "config/channels.yaml",
    )
    queue = f"zeus-control-e2e-{uuid.uuid4().hex[:8]}"
    auth = AuthConfig(password_hash=hash_password(WB_PASSWORD, iterations=1000))
    system = build_system(settings, temporal_client=temporal_env.client, task_queue=queue, auth=auth, env={})
    app = create_app(settings, system=system)
    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(transport=transport, base_url="http://zeus")

    # thin worker thật (zeus_worker) với bộ test cấu hình sẵn cho test.run
    repo = tmp_path / "work" / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_gia.py").write_text("def test_tinh_gia():\n    assert round(100 * 1.08, 2) == 108.0\n")
    token = await system.worker_services.registry.issue_token("w-e2e")
    wcfg = WorkerConfig(
        server_url="http://zeus", worker_id="w-e2e", work_dir=tmp_path / "work", extra_capabilities=["python"],
        repo_roots=[tmp_path / "work"], test_repo=repo, test_target="tests", heartbeat_interval_s=0.3, poll_wait_s=1,
    )
    wclient = WorkerApiClient("http://zeus", token, transport=transport)
    agent = WorkerAgent(wcfg, wclient)

    stop = asyncio.Event()
    control = asyncio.create_task(run_control_worker(settings, stop, client=temporal_env.client, task_queue=queue, sweep_interval_s=0.5, system=system))
    worker = asyncio.create_task(agent.run_forever(stop))
    for _ in range(100):  # worker đã register + heartbeat
        if [w for w in await system.worker_services.registry.list() if w.worker_id == "w-e2e"] and await system.worker_services.registry.last_heartbeat("w-e2e"):
            break
        await asyncio.sleep(0.1)
    try:
        yield Stack(system, app, http, pg_dsn, temporal_env)
    finally:
        stop.set()
        await asyncio.wait_for(asyncio.gather(control, worker, return_exceptions=True), 30)
        await wclient.aclose()
        await http.aclose()


async def test_e2e_event_to_worker_to_evidence_to_workbench(stack: Stack) -> None:
    res = await stack.ingest("Sửa endpoint backend API tính giá rồi chạy kiểm thử", "e2e-backend-1")
    task_id = res["task_id"]
    assert res["duplicate"] is False and res["workflow_id"] == task_id
    # idempotency: cùng external_id => cùng task, không workflow thứ hai
    again = await stack.ingest("Sửa endpoint backend API tính giá rồi chạy kiểm thử", "e2e-backend-1")
    assert again["duplicate"] is True and again["task_id"] == task_id

    verdict = await stack.verdict(task_id)
    assert verdict.decision is VerdictDecision.PASS and verdict.outcome is Outcome.VERIFIED_SUCCESS, verdict

    # Control API: task + node
    r = await stack.http.get(Paths.TASK.format(task_id=task_id), headers={HEADER_TENANT: TENANT})
    body = r.json()
    assert body["task"]["status"] == TaskStatus.SUCCEEDED.value and body["task"]["family"] == "backend"
    assert {n["node_id"]: n["status"] for n in body["nodes"]}["test"] == "SUCCEEDED"

    # C: scheduler đã chọn worker thật, assignment hoàn thành, activity đã được hoàn thành bằng task token
    asg = stack.q("SELECT worker_id, status, activity_completed, schedule_decision_id, payload FROM assignments WHERE task_id=%s", task_id)
    assert len(asg) == 1 and asg[0]["worker_id"] == "w-e2e" and asg[0]["status"] == "COMPLETED" and asg[0]["activity_completed"]
    assert asg[0]["payload"]["action"]["name"] == "test.run"
    dec = stack.q("SELECT decision_id, worker_id, outcome, features FROM schedule_decisions WHERE task_id=%s", task_id)
    assert len(dec) == 1 and dec[0]["worker_id"] == "w-e2e" and dec[0]["features"]
    assert dec[0]["decision_id"] == asg[0]["schedule_decision_id"]
    assert dec[0]["outcome"] == Outcome.VERIFIED_SUCCESS.value  # nhãn học ghi ngược vào quyết định lập lịch

    # B: EvidenceRecord + Outcome + artifact log của worker đã liên kết
    ev = stack.q("SELECT record_id, final_outcome, strength, payload FROM evidence_records WHERE task_id=%s", task_id)
    assert len(ev) == 1 and ev[0]["final_outcome"] == "VERIFIED_SUCCESS" and ev[0]["strength"] == "strong"
    payload = ev[0]["payload"]
    assert payload["worker_id"] == "w-e2e" and payload["schedule_decision_id"] == dec[0]["decision_id"]
    test_items = [e for e in payload["evidence"] if e["kind"] == "test_result"]
    assert test_items and test_items[0]["passed"] is True and test_items[0]["summary"].startswith("1 passed")
    links = stack.q("SELECT sha256 FROM evidence_artifact_links WHERE record_id=%s", ev[0]["record_id"])
    assert links and await stack.system.artifacts.verify(TENANT, links[0]["sha256"])
    out = stack.q("SELECT outcome, worker_id FROM outcomes WHERE task_id=%s", task_id)
    assert out == [{"outcome": "VERIFIED_SUCCESS", "worker_id": "w-e2e"}]
    assert stack.q("SELECT stage FROM dataset_records WHERE evidence_record_id=%s", ev[0]["record_id"]) == [{"stage": "VERIFIED"}]
    r = await stack.http.get(Paths.EVIDENCE.format(record_id=ev[0]["record_id"]), headers={HEADER_TENANT: TENANT})
    assert r.status_code == 200 and r.json()["final_outcome"] == "VERIFIED_SUCCESS"
    r = await stack.http.get(Paths.EVIDENCE.format(record_id=ev[0]["record_id"]), headers={HEADER_TENANT: "khac"})
    assert r.status_code == 404  # cô lập tenant (CCR EvidenceStore.get tenant_id)
    assert stack.q("SELECT count(*) AS n FROM trace_spans WHERE task_id=%s AND name='task.lifecycle'", task_id) == [{"n": 1}]

    # D: Workbench hiển thị task + bằng chứng + DAG + worker
    wb = await stack.wb_login()
    try:
        assert (await wb.get("/wb")).status_code == 200
        for path in ("/wb/workflows", f"/wb/workflows/{task_id}", "/wb/evidence"):
            page = await wb.get(path)
            assert page.status_code == 200 and task_id in page.text, path
        assert "<svg" in (await wb.get(f"/wb/workflows/{task_id}")).text  # DAG từ task_nodes thật
        detail = await wb.get(f"/wb/evidence/{task_id}")
        assert detail.status_code == 200 and "Đã xác minh: đạt" in detail.text and "w-e2e" in detail.text
        assert payload["trace_id"] in detail.text and "1 passed" in detail.text
        workers_page = await wb.get("/wb/workers")
        assert workers_page.status_code == 200 and "w-e2e" in workers_page.text
        for path in ("/wb/audit", "/wb/agents", "/wb/costs", "/wb/errors", "/wb/learning", "/wb/brain?q=gia", "/wb/approvals"):
            assert (await wb.get(path)).status_code == 200, path
    finally:
        await wb.aclose()


async def test_e2e_r2_blocked_until_approval_decision(stack: Stack) -> None:
    res = await stack.ingest("Triển khai bản phát hành mới (deploy) lên máy staging", "e2e-deploy-1")
    task_id = res["task_id"]
    pending: list[dict[str, Any]] = []
    for _ in range(200):
        r = await stack.http.get(Paths.APPROVALS, params={"status": "PENDING"}, headers={HEADER_TENANT: TENANT})
        pending = [a for a in r.json()["items"] if a["task_id"] == task_id]
        if pending:
            break
        await asyncio.sleep(0.1)
    assert len(pending) == 1 and pending[0]["risk"] == "R2"
    for _ in range(50):
        task = (await stack.http.get(Paths.TASK.format(task_id=task_id), headers={HEADER_TENANT: TENANT})).json()["task"]
        if task["status"] == TaskStatus.AWAITING_APPROVAL.value:
            break
        await asyncio.sleep(0.1)
    assert task["family"] == "deployment" and task["status"] == TaskStatus.AWAITING_APPROVAL.value

    # Chặn: chờ thêm nhiều chu kỳ poll của worker -> vẫn KHÔNG có assignment, evidence hay node nào chạy
    await asyncio.sleep(2.0)
    assert stack.q("SELECT count(*) AS n FROM assignments WHERE task_id=%s", task_id) == [{"n": 0}]
    assert stack.q("SELECT count(*) AS n FROM evidence_records WHERE task_id=%s", task_id) == [{"n": 0}]
    assert stack.q("SELECT count(*) AS n FROM task_nodes WHERE task_id=%s AND status IN ('RUNNING','SCHEDULED','SUCCEEDED')", task_id) == [{"n": 0}]
    # tenant khác không duyệt được
    r = await stack.http.post(Paths.APPROVAL_DECISION.format(approval_id=pending[0]["approval_id"]), json={"status": "APPROVED", "decided_by": "human:x"}, headers={HEADER_TENANT: "khac"})
    assert r.status_code == 404

    # Người vận hành duyệt qua Workbench (D -> ControlFacade -> ApprovalStore PG + signal Temporal)
    wb = await stack.wb_login()
    try:
        page = await wb.get("/wb/approvals")
        assert pending[0]["approval_id"] in page.text
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
        r = await wb.post(f"/wb/approvals/{pending[0]['approval_id']}/decision", data={"csrf_token": csrf, "decision": "approve", "comment": "ok staging"})
        assert r.status_code == 303 and r.headers["location"].endswith("msg=approved")
    finally:
        await wb.aclose()

    verdict = await stack.verdict(task_id)
    assert verdict.decision is VerdictDecision.PASS
    asg = stack.q("SELECT worker_id, status FROM assignments WHERE task_id=%s", task_id)
    assert asg == [{"worker_id": "w-e2e", "status": "COMPLETED"}]  # chỉ chạy SAU khi duyệt
    ev = stack.q("SELECT payload FROM evidence_records WHERE task_id=%s", task_id)
    assert ev[0]["payload"]["human_intervention"] is True
    appr = stack.q("SELECT status, decided_by FROM approvals WHERE approval_id=%s", pending[0]["approval_id"])
    assert appr == [{"status": "APPROVED", "decided_by": "human:operator"}]


async def test_e2e_r2_rejected_never_dispatches(stack: Stack) -> None:
    res = await stack.ingest("Deploy bản phát hành thử nghiệm lên staging", "e2e-deploy-2")
    task_id = res["task_id"]
    for _ in range(200):
        r = await stack.http.get(Paths.APPROVALS, params={"status": "PENDING"}, headers={HEADER_TENANT: TENANT})
        pending = [a for a in r.json()["items"] if a["task_id"] == task_id]
        if pending:
            break
        await asyncio.sleep(0.1)
    r = await stack.http.post(
        Paths.APPROVAL_DECISION.format(approval_id=pending[0]["approval_id"]),
        json={"status": "REJECTED", "decided_by": "human:huyen", "comment": "chưa tới lịch"}, headers={HEADER_TENANT: TENANT},
    )
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    verdict = await stack.verdict(task_id)
    assert verdict.decision is not VerdictDecision.PASS
    assert stack.q("SELECT count(*) AS n FROM assignments WHERE task_id=%s", task_id) == [{"n": 0}]
    task = (await stack.http.get(Paths.TASK.format(task_id=task_id), headers={HEADER_TENANT: TENANT})).json()["task"]
    assert task["status"] in (TaskStatus.FAILED.value, TaskStatus.CANCELLED.value)
