"""Hồi quy review round 2 (độ tin cậy, A/B): R1 R2 R3 R4 R6 R7 R8 R9 R11. Postgres/Temporal thật khi cần, không mạng ngoài."""

from __future__ import annotations

import asyncio
import dataclasses
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from tests.control_plane.conftest import make_rig, make_task
from tests.control_plane.test_stores_api import ev, make_client
from tests.control_plane.test_workflow import Harness
from zeus.contracts.api import AssignmentResult, Paths, TaskAssignment
from zeus.contracts.interfaces import ApprovalRequired
from zeus.contracts.models import (
    ActionResult,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    EvidenceItem,
    EvidenceKind,
    Outcome,
    RiskLevel,
    TaskFamily,
    TaskNode,
    TaskStatus,
    TraceContext,
    TypedAction,
    VerdictDecision,
    WorkerHeartbeat,
    utcnow,
)
from zeus.orchestration import types as T
from zeus.orchestration.activities import ControlActivities, assignment_id
from zeus.orchestration.client import signal_cancel
from zeus.policy.gateway import DefaultToolGateway, RemoteToolProvider
from zeus.policy.idempotency import PgIdempotency
from zeus.storage import apply_migrations

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


@pytest.fixture()
async def rig(models_cfg, policy_cfg):
    return await make_rig(models_cfg, policy_cfg)


# ---------------------------------------------------------------- R1
@pytest.mark.temporal
async def test_r1_node_exception_cannot_yield_verified_success(temporal_env, rig, monkeypatch):
    orig = ControlActivities._run_node

    async def boom(self, task, node):  # noqa: ANN001
        if node.node_id == "report":
            raise ApplicationError("worker không phù hợp cho report", non_retryable=True)
        return await orig(self, task, node)

    monkeypatch.setattr(ControlActivities, "_run_node", boom)
    task = make_task()
    async with Harness(temporal_env, rig) as h:
        verdict = await h.run(task)
    assert verdict.decision is not VerdictDecision.PASS and verdict.outcome is not Outcome.VERIFIED_SUCCESS
    assert "report" in " ".join(verdict.reasons) and "worker không phù hợp" in " ".join(verdict.reasons)  # nguyên nhân gốc, không chỉ 'Activity task failed'
    assert (await rig.store.get_task("zeusvn", task.task_id)).status in (TaskStatus.FAILED, TaskStatus.ROLLED_BACK)  # saga hoàn tác implement
    assert {n["node_id"]: n["status"] for n in await rig.store.list_nodes(task.task_id)}["report"] == "FAILED"
    assert rig.outcomes.dataset[0].outcome is not Outcome.VERIFIED_SUCCESS


# ---------------------------------------------------------------- R2
@pytest.mark.temporal
async def test_r2_cancel_assignment_failure_does_not_kill_workflow(temporal_env, rig):
    async def broken_cancel(aid: str) -> None:
        raise RuntimeError("queue down")

    rig.queue.cancel = broken_cancel  # type: ignore[method-assign]
    task = make_task()
    hold = asyncio.Event()
    async with Harness(temporal_env, rig, hold=hold) as h:
        handle = await h.start(task, max_activity_attempts=2, retry_initial_s=0.1)
        for _ in range(100):
            if rig.queue.owner:
                break
            await asyncio.sleep(0.1)
        await signal_cancel(temporal_env.client, task.task_id, "huỷ")
        verdict = await asyncio.wait_for(handle.result(), 60)  # trước đây: WorkflowFailureError
        hold.set()
    assert verdict.decision is VerdictDecision.FAIL
    assert (await rig.store.get_task("zeusvn", task.task_id)).status is TaskStatus.CANCELLED


@pytest.mark.pg
async def test_r2_cancel_assignments_skips_attempts_that_do_not_exist(pg_dsn, rig):
    from zeus.workers.queue import PgAssignmentQueue

    apply_migrations(pg_dsn, MIGRATIONS)
    q = PgAssignmentQueue(pg_dsn)
    rig.deps.queue = q
    task = make_task()
    a1 = TaskAssignment(assignment_id=assignment_id(task.task_id, "test", 1), task_id=task.task_id, node_id="test",
                        action=TypedAction(name="test.run"), lease_expires_at=utcnow() + timedelta(seconds=60), trace=TraceContext(trace_id="a" * 32))
    await q.enqueue(a1, "w1", b"T")
    n = await ControlActivities(rig.deps).cancel_assignments(task, ["test", "node-local-khong-co-assignment"])  # trước đây: KeyError a2
    assert n == 1 and (await q.status(a1.assignment_id))["status"] == "CANCELLED"


# ---------------------------------------------------------------- R3
async def test_r3_ingest_resumes_after_post_commit_failure(rig):
    gw = rig.event_gateway
    real = gw.intent.classify
    calls = {"n": 0}

    async def flaky(event):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("LLM down")
        return await real(event)

    gw.intent.classify = flaky  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await gw.ingest(ev(external_id="r3-a"))
    assert rig.store.tasks == {}  # event đã lưu, chưa có task
    res = await gw.ingest(ev(external_id="r3-a"))  # nền tảng retry cùng external_id
    assert res.task is not None and not res.duplicate and len(rig.store.tasks) == 1  # trước đây: duplicate, task_id=None, event mất
    again = await gw.ingest(ev(external_id="r3-a"))
    assert again.duplicate and again.task_id == res.task.task_id and len(rig.store.tasks) == 1


async def test_r3_duplicate_ingest_restarts_workflow_for_pending_task(rig):
    client, wf = make_client(rig)
    rig.event_gateway.resume_after_s = 0.05  # PENDING quá ngưỡng => workflow lần đầu có thể chưa start được
    body = {"event": ev(external_id="r3-b").model_dump(mode="json")}
    r1 = client.post(Paths.EVENTS, json=body, headers={"X-Zeus-Tenant": "zeusvn"})
    assert r1.status_code == 200 and r1.json()["duplicate"] is False
    tid = r1.json()["task_id"]
    # giả lập: start workflow lần đầu thất bại (Temporal tạm mất) => task vẫn PENDING; nền tảng gửi lại
    body["event"]["event_id"] = "evt_retry"
    await asyncio.sleep(0.1)
    r2 = client.post(Paths.EVENTS, json=body, headers={"X-Zeus-Tenant": "zeusvn"})
    assert r2.status_code == 200 and r2.json()["duplicate"] is True and r2.json()["task_id"] == tid
    assert wf.started == [tid, tid]  # start lại (idempotent theo workflow id) thay vì bỏ rơi task PENDING
    await rig.store.set_task_status(tid, TaskStatus.RUNNING)
    client.post(Paths.EVENTS, json=body, headers={"X-Zeus-Tenant": "zeusvn"})
    assert wf.started == [tid, tid]  # task đã chạy: không start lại


@pytest.mark.pg
async def test_r3_sweeper_restarts_workflow_of_old_pending_tasks(pg_dsn, rig):
    from zeus.api.router import ControlServices, NullWorkflowControl
    from zeus.app.system import ControlFacade
    from zeus.gateway.store import PgControlStore

    apply_migrations(pg_dsn, MIGRATIONS)
    store = PgControlStore(pg_dsn)
    wf = NullWorkflowControl()
    facade = ControlFacade(ControlServices(rig.event_gateway, store, rig.approvals, rig.evidence, rig.outcomes, rig.workers, wf))
    old = make_task().model_copy(update={"created_at": utcnow() - timedelta(minutes=5)})
    fresh = make_task()
    running = make_task().model_copy(update={"created_at": utcnow() - timedelta(minutes=5), "status": TaskStatus.RUNNING})
    for t in (old, fresh, running):
        await store.put_task(t)
    assert await facade.resume_pending(older_than_s=60) == [old.task_id]
    assert wf.started == [old.task_id]


# ---------------------------------------------------------------- R4
def _judge_req(task, finished_ms: int) -> T.JudgeRequest:
    node = TaskNode(node_id="test", title="t", action=TypedAction(name="test.run", tenant_id=task.tenant_id, task_id=task.task_id))
    res = AssignmentResult(
        assignment_id="a1", worker_id="w1",
        result=ActionResult(action_id=node.action.action_id, ok=True, executed_by="w1"),
        evidence=[EvidenceItem(kind=EvidenceKind.TEST_RESULT, summary="5 passed", passed=True)],
    )
    return T.JudgeRequest(task=task, results=[T.NodeRun(node=node, result=res)], started_at_ms=max(0, finished_ms - 500), finished_at_ms=finished_ms)


@pytest.mark.pg
@pytest.mark.parametrize("finished_ms", [0, 1_700_000_000_000])
async def test_r4_judge_and_record_is_idempotent_across_temporal_retry(pg_dsn, rig, finished_ms):
    import psycopg

    from zeus.evidence.store import PgEvidenceStore
    from zeus.learning.outcomes import PgOutcomeRecorder

    apply_migrations(pg_dsn, MIGRATIONS)
    evs = PgEvidenceStore(pg_dsn)
    real = PgOutcomeRecorder(pg_dsn, evs)
    state = {"n": 0}

    class FlakyOutcomes:
        async def record(self, evidence):  # noqa: ANN001
            state["n"] += 1
            if state["n"] == 1:
                raise ConnectionError("postgres đứt kết nối")
            return await real.record(evidence)

    rig.deps.evidence, rig.deps.outcomes = evs, FlakyOutcomes()  # type: ignore[assignment]
    acts = ControlActivities(rig.deps)
    task = make_task()
    req = _judge_req(task, finished_ms)
    with pytest.raises(ConnectionError):
        await acts.judge_and_record(req)
    if finished_ms == 0:
        await asyncio.sleep(0.01)  # created_at mặc định (utcnow) sẽ khác giữa hai lần
    out = await acts.judge_and_record(req)  # attempt 2: trước đây EvidenceImmutable
    assert out.verdict.decision is VerdictDecision.PASS and out.verdict.outcome is Outcome.VERIFIED_SUCCESS
    with psycopg.connect(pg_dsn) as c:
        assert c.execute("SELECT count(*) FROM outcomes WHERE task_id=%s", (task.task_id,)).fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM evidence_records WHERE task_id=%s", (task.task_id,)).fetchone()[0] == 1


# ---------------------------------------------------------------- R6
@pytest.mark.pg
async def test_r6_retry_attempt_reuses_finished_result_and_cancels_stale_attempt(pg_dsn, rig):
    from zeus.workers.queue import PgAssignmentQueue

    apply_migrations(pg_dsn, MIGRATIONS)
    q = PgAssignmentQueue(pg_dsn)
    rig.deps.queue = q
    acts = ControlActivities(rig.deps)
    task = make_task(family=TaskFamily.BACKEND)
    node = TaskNode(node_id="test", title="t", action=TypedAction(name="test.run", tenant_id=task.tenant_id, task_id=task.task_id, args={"goal": "g"}),
                    required_capabilities=["python"])

    def env(attempt: int) -> ActivityEnvironment:
        e = ActivityEnvironment()
        e.info = dataclasses.replace(e.info, attempt=attempt, task_token=f"T{attempt}".encode(), start_to_close_timeout=timedelta(seconds=900),
                                     started_time=datetime.now(timezone.utc))
        return e

    def asg(attempt: int) -> TaskAssignment:
        return TaskAssignment(assignment_id=assignment_id(task.task_id, "test", attempt), task_id=task.task_id, node_id="test", action=node.action,
                              lease_expires_at=utcnow() + timedelta(seconds=60), trace=TraceContext(trace_id="a" * 32), attempt=attempt)

    # (a) lần 1 đã chạy xong nhưng kết quả không về được activity (R5): lần 2 phải dùng lại, KHÔNG enqueue/chạy lại hành động ngoài
    await q.enqueue(asg(1), "w1", b"T1")
    await q.poll("w1")
    await q.complete(AssignmentResult(assignment_id=asg(1).assignment_id, worker_id="w1", result=ActionResult(action_id=node.action.action_id, ok=True, output={"ran": 1})))
    got = await env(2).run(acts.run_node, task, node)
    assert got.result.ok and got.result.output == {"ran": 1}
    assert await q.status(assignment_id(task.task_id, "test", 2)) is None
    assert (await q.status(asg(1).assignment_id))["activity_completed"] is True

    # (b) lần 1 còn QUEUED/LEASED khi activity timeout: lần 2 huỷ nó trước khi enqueue (không chạy trùng)
    task2 = make_task(family=TaskFamily.BACKEND)
    node2 = node.model_copy(update={"action": node.action.model_copy(update={"task_id": task2.task_id, "action_id": "act_other"})})

    def asg2(attempt: int) -> TaskAssignment:
        return asg(attempt).model_copy(update={"assignment_id": assignment_id(task2.task_id, "test", attempt), "task_id": task2.task_id, "action": node2.action})

    await q.enqueue(asg2(1), "w1", b"T1")
    with pytest.raises(BaseException) as ei:  # raise_complete_async
        await env(2).run(acts.run_node, task2, node2)
    assert type(ei.value).__name__ == "_CompleteAsyncError"
    assert (await q.status(asg2(1).assignment_id))["status"] == "CANCELLED"
    assert (await q.status(asg2(2).assignment_id))["status"] == "QUEUED"


# ---------------------------------------------------------------- R7
@pytest.mark.pg
async def test_r7_tool_idempotency_survives_gateway_restart(pg_dsn, rig):
    from tests.control_plane.conftest import worker_specs

    apply_migrations(pg_dsn, MIGRATIONS)
    idem = PgIdempotency(pg_dsn)

    def new_gateway() -> DefaultToolGateway:  # "restart": object mới, cùng DB + cùng kho duyệt
        return DefaultToolGateway([rig.control_tool, RemoteToolProvider(worker_specs())], rig.gateway.policy, rig.approvals, rig.audit, idempotency=idem)

    act = TypedAction(name="channel.send_message", args={"text": "Chào"}, idempotency_key="tsk_1:send")
    gw1 = new_gateway()
    with pytest.raises(ApprovalRequired) as ei:
        await gw1.execute(act)
    await rig.approvals.decide(ApprovalDecision(approval_id=ei.value.request.approval_id, status=ApprovalStatus.APPROVED, decided_by="human:h"))
    r1 = await gw1.execute(act)
    assert r1.ok and len(rig.control_tool.executed) == 1
    r2 = await new_gateway().execute(act)  # trước đây: gửi lần 2
    assert r2.ok and len(rig.control_tool.executed) == 1 and r2.action_id == r1.action_id


@pytest.mark.pg
async def test_r7_unknown_outcome_is_not_rerun_but_known_failure_is_retryable(pg_dsn, rig):
    from tests.control_plane.conftest import worker_specs

    apply_migrations(pg_dsn, MIGRATIONS)
    idem = PgIdempotency(pg_dsn)
    gw = DefaultToolGateway([rig.control_tool, RemoteToolProvider(worker_specs())], rig.gateway.policy, rig.approvals, rig.audit, idempotency=idem)
    act = TypedAction(name="channel.send_message", args={"text": "Chào"}, idempotency_key="k-unknown")
    with pytest.raises(ApprovalRequired) as ei:
        await gw.execute(act)
    await rig.approvals.decide(ApprovalDecision(approval_id=ei.value.request.approval_id, status=ApprovalStatus.APPROVED, decided_by="human:h"))
    assert (await idem.begin("zeusvn", "k-unknown"))[0] == "new"  # process cũ chiếm khoá rồi chết giữa chừng
    r = await gw.execute(act)
    assert not r.ok and "không rõ" in (r.error or "") and rig.control_tool.executed == []
    # thất bại đã biết => nhả khoá => chạy lại được
    calls = {"n": 0}

    async def flaky(a):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("HTTP 500")
        return {"ok": 1}

    rig.control_tool._handler = flaky  # type: ignore[attr-defined]
    act2 = TypedAction(name="channel.send_message", args={"text": "Hai"}, idempotency_key="k-retry")
    with pytest.raises(ApprovalRequired) as ei2:
        await gw.execute(act2)
    await rig.approvals.decide(ApprovalDecision(approval_id=ei2.value.request.approval_id, status=ApprovalStatus.APPROVED, decided_by="human:h"))
    # FakeToolProvider không bắt exception => gateway trả ok=False và nhả khoá
    assert not (await gw.execute(act2)).ok
    assert (await gw.execute(act2)).ok and calls["n"] == 2


# ---------------------------------------------------------------- R8
def _silent_server() -> tuple[socket.socket, int]:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    conns: list[socket.socket] = []

    def accept() -> None:
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            conns.append(c)  # nhận kết nối nhưng không bao giờ trả lời (Postgres treo)

    threading.Thread(target=accept, daemon=True).start()
    return srv, srv.getsockname()[1]


def test_r8_pg_connect_has_default_timeout(monkeypatch):
    import psycopg

    from zeus.storage.db import connect

    monkeypatch.setenv("ZEUS_DB_CONNECT_TIMEOUT", "1")
    srv, port = _silent_server()
    try:
        t0 = time.monotonic()
        with pytest.raises(psycopg.OperationalError):
            connect(f"host=127.0.0.1 port={port} user=x dbname=x")
        assert time.monotonic() - t0 < 8  # trước đây: treo vô hạn
    finally:
        srv.close()


async def test_r8_span_export_does_not_block_event_loop_when_postgres_hangs():
    from zeus.obs.spans import Span, SpanKind, SpanStatus
    from zeus.storage.spans import PgSpanExporter

    srv, port = _silent_server()
    try:
        exp = PgSpanExporter(f"host=127.0.0.1 port={port} user=x dbname=x", background=True, connect_timeout=1)
        span = Span(trace_id="a" * 32, span_id="b" * 16, name="x", kind=SpanKind.INTERNAL, start_time_unix_nano=1, end_time_unix_nano=2, status_code=SpanStatus.OK)
        ticks = {"n": 0}

        async def ticker() -> None:
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.05)

        t = asyncio.create_task(ticker())
        t0 = time.monotonic()
        exp.export([span])
        assert time.monotonic() - t0 < 0.5  # chỉ xếp hàng
        await asyncio.sleep(0.5)
        t.cancel()
        assert ticks["n"] >= 5  # event loop vẫn chạy trong lúc DB treo
    finally:
        srv.close()


# ---------------------------------------------------------------- R9
@pytest.mark.temporal
async def test_r9_no_worker_waits_longer_than_retry_policy(temporal_env, models_cfg, policy_cfg):
    rig = await make_rig(models_cfg, policy_cfg, register_worker=False)
    rig.deps.no_worker_wait_s, rig.deps.no_worker_poll_s = 20.0, 0.1
    task = make_task()
    async with Harness(temporal_env, rig) as h:
        # retry policy chỉ 2 lần x 0.1s: trước đây node thất bại sau <1s dù worker chỉ vắng ít giây
        handle = await h.start(task, retry_initial_s=0.1, max_activity_attempts=2)
        await asyncio.sleep(2.0)
        assert (await handle.describe()).status.name == "RUNNING" and not rig.queue.owner
        from tests.control_plane.conftest import make_worker

        await rig.workers.register(make_worker())
        await rig.workers.heartbeat(WorkerHeartbeat(worker_id="w1", ram_available_mb=4096))
        verdict = await asyncio.wait_for(handle.result(), 60)
    assert verdict.decision is VerdictDecision.PASS


# ---------------------------------------------------------------- R11
@pytest.mark.temporal
async def test_r11_cancel_while_awaiting_approval_expires_pending_approvals(temporal_env, rig):
    task = make_task("Chạy migration cho bảng đơn hàng", family=TaskFamily.DATABASE)
    async with Harness(temporal_env, rig) as h:
        handle = await h.start(task)
        await h.wait_phase(handle, "AWAITING_APPROVAL")
        assert [a.status for a in await rig.approvals.list("zeusvn")] == [ApprovalStatus.PENDING]
        await signal_cancel(temporal_env.client, task.task_id, "huỷ")
        verdict = await asyncio.wait_for(handle.result(), 60)
    assert verdict.decision is VerdictDecision.FAIL
    assert [a.status for a in await rig.approvals.list("zeusvn")] == [ApprovalStatus.EXPIRED]  # trước đây: PENDING (zombie)


async def test_r11_deciding_approval_of_finished_task_is_409_and_not_recorded(rig):
    client, wf = make_client(rig)
    task = make_task("x", family=TaskFamily.DATABASE).model_copy(update={"status": TaskStatus.CANCELLED})
    await rig.store.put_task(task)
    req = await rig.approvals.request(ApprovalRequest(
        tenant_id="zeusvn", task_id=task.task_id, action=TypedAction(name="db.migrate", task_id=task.task_id), risk=RiskLevel.R2, summary_vi="duyệt"))
    r = client.post(Paths.APPROVAL_DECISION.format(approval_id=req.approval_id), json={"status": "APPROVED", "decided_by": "human:h"}, headers={"X-Zeus-Tenant": "zeusvn"})
    assert r.status_code == 409
    assert (await rig.approvals.get(req.approval_id)).status is ApprovalStatus.EXPIRED and wf.approvals == []


async def test_r11_approval_timeout_comes_from_policy(rig):
    cfg = rig.gateway.policy.config
    rig.gateway.policy.config = cfg.model_copy(update={"approval_timeout_s": 77})
    acts = ControlActivities(rig.deps)
    task = make_task("Chạy migration", family=TaskFamily.DATABASE)
    bundle = await acts.prepare_approvals(task, rig.deps.planner.playbook_plan(task), None)
    assert bundle.timeout_s == 77 and bundle.requests
    assert (await acts.prepare_approvals(task, rig.deps.planner.playbook_plan(task), 5)).timeout_s == 5
