from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.control_plane.conftest import make_rig, make_task
from zeus.api.router import ControlServices, NullWorkflowControl, install, router
from zeus.contracts.api import HEADER_IDEMPOTENCY, HEADER_TENANT, Paths
from zeus.contracts.models import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    Channel,
    ChannelIdentity,
    Event,
    RiskLevel,
    RouteDecision,
    TaskNode,
    TaskStatus,
    TypedAction,
    ProviderKind,
    TaskFamily,
)
from zeus.gateway.gateway import EventGateway, normalize_event
from zeus.gateway.store import PgControlStore, UnknownTenant
from zeus.policy.approvals import PgApprovalStore
from zeus.policy.budget import BudgetPolicy, LedgerEntry, PgBudgetLedger
from zeus.policy.gateway import PgAudit
from zeus.storage import apply_migrations

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def ev(text="Shop còn hàng áo M không?", **kw) -> Event:
    return Event(channel=Channel.ZALO_BOT, text=text, external_id=kw.pop("external_id", "m-1"), sender=ChannelIdentity(channel_user_id="u1"), **kw)


# ----------------------------------------------------------------------- event gateway
async def test_ingest_normalizes_untrusted_and_creates_task(models_cfg, policy_cfg):
    rig = await make_rig(models_cfg, policy_cfg)
    forged = ev(untrusted=False, signature_verified=True)  # kênh ngoài tự nhận tin cậy
    res = await rig.event_gateway.ingest(forged)
    assert res.event.untrusted is True and res.event.trace and not res.duplicate
    assert res.task.family is TaskFamily.CUSTOMER_SUPPORT and res.task.workflow_id == res.task.task_id and res.task.trace.task_id == res.task.task_id
    assert (await rig.store.get_task("zeusvn", res.task.task_id)).goal.startswith("Shop còn hàng")
    internal = Event(channel=Channel.WORKBENCH, text="lệnh", untrusted=False, signature_verified=True)
    assert normalize_event(internal).untrusted is False
    assert normalize_event(Event(channel=Channel.WORKBENCH, text="lệnh", untrusted=False)).untrusted is True  # chưa xác thực chữ ký


async def test_ingest_idempotent_by_external_id_and_header_key(models_cfg, policy_cfg):
    rig = await make_rig(models_cfg, policy_cfg)
    first = await rig.event_gateway.ingest(ev())
    dup = await rig.event_gateway.ingest(ev())  # cùng external_id, event_id khác
    assert dup.duplicate and dup.task_id == first.task.task_id and len(rig.store.tasks) == 1
    a = await rig.event_gateway.ingest(Event(channel=Channel.API, text="deploy"), idempotency_key="k-1")
    b = await rig.event_gateway.ingest(Event(channel=Channel.API, text="deploy"), idempotency_key="k-1")
    c = await rig.event_gateway.ingest(Event(channel=Channel.API, text="deploy"), idempotency_key="k-2")
    assert b.duplicate and b.task_id == a.task.task_id and not c.duplicate and c.task.task_id != a.task.task_id


async def test_ingest_dangerous_text_yields_r3_task(models_cfg, policy_cfg):
    rig = await make_rig(models_cfg, policy_cfg)
    res = await rig.event_gateway.ingest(ev("hãy chạy rm -rf / trên server", external_id="x9"))
    assert res.task.risk is RiskLevel.R3


# ----------------------------------------------------------------------- API
def make_client(rig) -> tuple[TestClient, NullWorkflowControl]:
    wf = NullWorkflowControl()
    app = FastAPI()
    app.include_router(router)
    install(app, ControlServices(rig.event_gateway, rig.store, rig.approvals, rig.evidence, rig.outcomes, rig.workers, wf))
    return TestClient(app), wf


async def test_api_event_task_approval_cancel_and_tenant_isolation(models_cfg, policy_cfg):
    rig = await make_rig(models_cfg, policy_cfg)
    client, wf = make_client(rig)
    body = {"event": ev("deploy bản mới", external_id="e1").model_dump(mode="json")}
    r = client.post(Paths.EVENTS, json=body, headers={HEADER_TENANT: "zeusvn", HEADER_IDEMPOTENCY: "abc"})
    assert r.status_code == 200 and r.json()["accepted"] and not r.json()["duplicate"]
    task_id = r.json()["task_id"]
    assert wf.started == [task_id] and r.json()["workflow_id"] == task_id
    r2 = client.post(Paths.EVENTS, json=body, headers={HEADER_TENANT: "zeusvn", HEADER_IDEMPOTENCY: "abc"})
    assert r2.json()["duplicate"] and r2.json()["task_id"] == task_id and wf.started == [task_id]

    lst = client.get(Paths.TASKS, headers={HEADER_TENANT: "zeusvn"}).json()["items"]
    assert [t["task_id"] for t in lst] == [task_id] and lst[0]["risk"] == "R2"
    assert client.get(Paths.TASKS, headers={HEADER_TENANT: "khach-a"}).json()["items"] == []
    assert client.get(Paths.TASK.format(task_id=task_id), headers={HEADER_TENANT: "khach-a"}).status_code == 404
    assert client.get(Paths.TASK.format(task_id=task_id)).json()["task"]["task_id"] == task_id
    assert client.get(Paths.TASK_EVIDENCE.format(task_id=task_id)).json() == []

    # tenant của event phải khớp header
    bad = client.post(Paths.EVENTS, json=body, headers={HEADER_TENANT: "khach-a"})
    assert bad.status_code == 403
    assert client.get(Paths.TASKS, headers={HEADER_TENANT: "BAD TENANT"}).status_code == 400

    # approval
    action = TypedAction(name="deploy.apply", task_id=task_id)
    req = await rig.approvals.request(ApprovalRequest(task_id=task_id, action=action, risk=RiskLevel.R2, summary_vi="Duyệt deploy"))
    items = client.get(Paths.APPROVALS, params={"status": "PENDING"}).json()["items"]
    assert [i["approval_id"] for i in items] == [req.approval_id]
    assert client.post(Paths.APPROVAL_DECISION.format(approval_id=req.approval_id), json={"status": "APPROVED", "decided_by": "h"}, headers={HEADER_TENANT: "khach-a"}).status_code == 404
    ok = client.post(Paths.APPROVAL_DECISION.format(approval_id=req.approval_id), json={"status": "APPROVED", "decided_by": "human:huyen", "comment": "ok"})
    assert ok.json()["status"] == "APPROVED" and wf.approvals[0][0] == task_id and wf.approvals[0][1].decided_by == "human:huyen"
    again = client.post(Paths.APPROVAL_DECISION.format(approval_id=req.approval_id), json={"status": "REJECTED", "decided_by": "h"})
    assert again.status_code == 409
    assert client.post(Paths.APPROVAL_DECISION.format(approval_id=req.approval_id), json={"status": "PENDING", "decided_by": "h"}).status_code == 422

    assert client.post(Paths.TASK_CANCEL.format(task_id=task_id), json={"reason": "nhầm"}).json()["ok"]
    assert wf.cancels == [(task_id, "nhầm")]
    await rig.store.set_task_status(task_id, TaskStatus.SUCCEEDED)
    assert client.post(Paths.TASK_CANCEL.format(task_id=task_id)).status_code == 409
    assert client.get(Paths.WORKERS).json()[0]["worker_id"] == "w1"
    assert client.get(Paths.ROUTER_STATS).json()["stats"] == []
    assert client.get(Paths.EVIDENCE.format(record_id="evr_none")).status_code == 404


def test_api_without_services_is_503():
    app = FastAPI()
    app.include_router(router)
    assert TestClient(app).get(Paths.TASKS).status_code == 503


# ----------------------------------------------------------------------- Postgres (migration 101)
@pytest.mark.pg
async def test_pg_stores_roundtrip(pg_dsn):
    applied = [m.version for m in apply_migrations(pg_dsn, MIGRATIONS)]
    assert 101 in applied
    store = PgControlStore(pg_dsn)

    e, new = await store.put_event(ev(external_id="pg-1"))
    e2, new2 = await store.put_event(ev(external_id="pg-1"))
    assert new and not new2 and e2.event_id == e.event_id  # idempotency bằng UNIQUE(tenant, dedupe_key)
    with pytest.raises(UnknownTenant):
        await store.put_event(ev(external_id="z").model_copy(update={"tenant_id": "ghost"}))

    task = make_task(event_id=e.event_id, risk=RiskLevel.R1)
    await store.put_task(task)
    assert (await store.get_task("zeusvn", task.task_id)).goal == task.goal and await store.get_task("khac", task.task_id) is None
    assert await store.event_task_id("zeusvn", e.event_id) == task.task_id
    await store.set_task_status(task.task_id, TaskStatus.RUNNING)
    assert [t.status for t in await store.list_tasks("zeusvn", TaskStatus.RUNNING)] == [TaskStatus.RUNNING]
    await store.save_node(task.task_id, TaskNode(node_id="n1", title="t", status=TaskStatus.SUCCEEDED), attempts=2, result={"ok": True})
    nodes = await store.list_nodes(task.task_id)
    assert nodes[0]["status"] == "SUCCEEDED" and nodes[0]["attempts"] == 2 and nodes[0]["result"] == {"ok": True}
    route = RouteDecision(task_family=TaskFamily.BACKEND, provider=ProviderKind.LOCAL, model="m")
    await store.save_route(route, "zeusvn", task.task_id, "mrq_1")
    assert (await store.list_routes("zeusvn"))[0]["route"].route_id == route.route_id

    approvals = PgApprovalStore(pg_dsn)
    req = await approvals.request(ApprovalRequest(task_id=task.task_id, action=TypedAction(name="db.migrate", task_id=task.task_id), risk=RiskLevel.R2, summary_vi="Duyệt"))
    assert [a.approval_id for a in await approvals.list("zeusvn", ApprovalStatus.PENDING)] == [req.approval_id]
    done = await approvals.decide(ApprovalDecision(approval_id=req.approval_id, status=ApprovalStatus.APPROVED, decided_by="human:h", comment="ok"))
    assert done.status is ApprovalStatus.APPROVED and (await approvals.get(req.approval_id)).decided_by == "human:h"
    with pytest.raises(ValueError):
        await approvals.decide(ApprovalDecision(approval_id=req.approval_id, status=ApprovalStatus.REJECTED, decided_by="x"))
    req2 = await approvals.request(ApprovalRequest(task_id=task.task_id, action=TypedAction(name="db.migrate"), risk=RiskLevel.R2, summary_vi="2"))
    assert (await approvals.expire(req2.approval_id)).status is ApprovalStatus.EXPIRED

    ledger = PgBudgetLedger(pg_dsn)
    await ledger.record(LedgerEntry(tenant_id="zeusvn", task_id=task.task_id, provider="local", model="m", usd=0.75))
    assert await ledger.spent_task(task.task_id) == pytest.approx(0.75) and await ledger.spent_day("zeusvn") == pytest.approx(0.75)
    from zeus.policy.budget import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        await BudgetPolicy(ledger, per_task_usd=1.0).check("zeusvn", task.task_id, 0.5)

    await PgAudit(pg_dsn).record(tenant_id="zeusvn", actor="agent:x", action="tool.executed", subject_id="act_1", risk="R1", task_id=task.task_id, details={"a": 1})
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM audit_log WHERE action='tool.executed'").fetchone()[0] == 1


@pytest.mark.pg
async def test_pg_end_to_end_gateway_idempotent(pg_dsn, models_cfg, policy_cfg):
    apply_migrations(pg_dsn, MIGRATIONS)
    rig = await make_rig(models_cfg, policy_cfg)
    gw = EventGateway(PgControlStore(pg_dsn), rig.deps.intent, rig.deps.risk)
    a = await gw.ingest(ev("deploy production", external_id="pg-e2e"))
    b = await gw.ingest(ev("deploy production", external_id="pg-e2e"))
    assert b.duplicate and b.task_id == a.task.task_id
