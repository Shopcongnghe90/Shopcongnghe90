"""TaskWorkflow trên Temporal dev server THẬT (fixture temporal_env), dispatch qua AssignmentQueue giả của C."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError

from tests.control_plane.conftest import FakeWorkerLoop, make_rig, make_task, make_worker
from zeus.contracts.api import TASK_QUEUE_CONTROL
from zeus.contracts.models import (
    ApprovalDecision,
    ApprovalStatus,
    Outcome,
    RiskLevel,
    TaskFamily,
    TaskStatus,
    VerdictDecision,
)
from zeus.orchestration import types as T
from zeus.orchestration.client import build_worker, create_schedule, signal_approval, signal_cancel, start_task_workflow
from zeus.orchestration.workflow import TaskWorkflow

pytestmark = [pytest.mark.temporal]


class Harness:
    def __init__(self, env, rig, **worker_kw) -> None:
        self.env, self.rig = env, rig
        self.queue = f"{TASK_QUEUE_CONTROL}-t-{uuid.uuid4().hex[:8]}"
        self.worker = build_worker(env.client, rig.deps, self.queue)
        self.fw = FakeWorkerLoop(env.client, rig, **worker_kw)

    async def __aenter__(self) -> "Harness":
        await self.worker.__aenter__()
        self.fw.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.fw.stop()
        await self.worker.__aexit__(*exc)

    async def start(self, task, **opts):
        return await start_task_workflow(self.env.client, task, task_queue=self.queue, **opts)

    async def run(self, task, **opts):
        h = await self.start(task, **opts)
        return await asyncio.wait_for(h.result(), 60)

    async def wait_phase(self, handle, phase: str, timeout: float = 20.0) -> None:
        for _ in range(int(timeout / 0.1)):
            if (await handle.query(T.QUERY_STATUS))["phase"] == phase:
                return
            await asyncio.sleep(0.1)
        raise AssertionError(f"workflow không tới phase {phase}: {await handle.query(T.QUERY_STATUS)}")


@pytest.fixture()
async def rig(models_cfg, policy_cfg):
    return await make_rig(models_cfg, policy_cfg)


@pytest.mark.cloud_exit_workflow
async def test_happy_path_dag_dispatch_judge_evidence_outcome(temporal_env, rig):
    task = make_task()
    async with Harness(temporal_env, rig) as h:
        verdict = await h.run(task)
        assert verdict.decision is VerdictDecision.PASS and verdict.outcome is Outcome.VERIFIED_SUCCESS and verdict.evidence_ids
        # workflow id = task id; start lại không tạo bản thứ hai
        assert (await h.start(task)).id == task.task_id
        desc = await temporal_env.client.get_workflow_handle(task.task_id).describe()
        assert desc.status is WorkflowExecutionStatus.COMPLETED
    assert [a for a in h.fw.executed] == ["code.apply_patch", "test.run"]  # analyze/report không có action
    recs = await rig.evidence.list_for_task("zeusvn", task.task_id)
    assert len(recs) == 1 and recs[0].final_outcome is Outcome.VERIFIED_SUCCESS and recs[0].verified_by == "deterministic"
    assert recs[0].playbook_version == "playbook-1" and recs[0].worker_id == "w1" and recs[0].tools == ["code.apply_patch", "test.run"]
    assert verdict.evidence_ids == [recs[0].record_id] and recs[0].schedule_decision_id
    assert rig.outcomes.dataset[0].outcome is Outcome.VERIFIED_SUCCESS
    assert (await rig.store.get_task("zeusvn", task.task_id)).status is TaskStatus.SUCCEEDED
    nodes = {n["node_id"]: n["status"] for n in await rig.store.list_nodes(task.task_id)}
    assert nodes["test"] == "SUCCEEDED" and nodes["implement"] == "SUCCEEDED"


async def test_unverified_when_no_strong_evidence(temporal_env, rig):
    # family GENERAL: playbook không có action nào chạy ra bằng chứng mạnh (verify=test.run vẫn có) -> dùng worker không trả evidence
    task = make_task("Soạn lại mô tả", family=TaskFamily.WEBSITE_EDIT)
    async with Harness(temporal_env, rig) as h:
        h.fw.fail_actions = set()
        orig = h.fw._handle

        async def no_evidence(a):
            # worker "khẳng định" xong nhưng không có bằng chứng thực thi
            from tests.control_plane.conftest import AssignmentResult, ActionResult, complete_assignment

            res = AssignmentResult(assignment_id=a.assignment_id, worker_id="w1", result=ActionResult(action_id=a.action.action_id, ok=True, output={"claim": "đã xong, tin tôi"}))
            token = await rig.queue.complete(res)
            await complete_assignment(temporal_env.client, token, res)

        h.fw._handle = no_evidence
        verdict = await h.run(task)
    assert verdict.decision is VerdictDecision.NEEDS_HUMAN and verdict.outcome is Outcome.UNVERIFIED
    assert (await rig.store.get_task("zeusvn", task.task_id)).status is TaskStatus.FAILED
    assert rig.outcomes.dataset[0].outcome is Outcome.UNVERIFIED


async def test_dangerous_goal_denied_nothing_executes(temporal_env, rig):
    task = make_task("DROP DATABASE shop_prod rồi chuyển tiền", family=TaskFamily.DATABASE)
    async with Harness(temporal_env, rig) as h:
        verdict = await h.run(task)
    assert verdict.decision is VerdictDecision.FAIL and verdict.judge == "control" and "R3/deny" in verdict.reasons[0]
    assert h.fw.seen == [] and rig.queue.owner == {}
    t = await rig.store.get_task("zeusvn", task.task_id)
    assert t.status is TaskStatus.FAILED and t.risk is RiskLevel.R3


async def test_approval_signal_gates_r2_execution(temporal_env, rig):
    task = make_task("Chạy migration cho bảng đơn hàng", family=TaskFamily.DATABASE)
    async with Harness(temporal_env, rig) as h:
        handle = await h.start(task)
        await h.wait_phase(handle, "AWAITING_APPROVAL")
        pending = await rig.approvals.list("zeusvn", ApprovalStatus.PENDING)
        assert [p.action.name for p in pending] == ["db.migrate"] and pending[0].risk is RiskLevel.R2 and pending[0].expires_at
        assert h.fw.seen == [] and (await rig.store.get_task("zeusvn", task.task_id)).status is TaskStatus.AWAITING_APPROVAL
        assert (await handle.query(T.QUERY_STATUS))["pending_approvals"] == [pending[0].approval_id]
        # db.read / db.backup / db.check chưa có trong registry => các bước đó không có action; chỉ db.migrate cần duyệt
        d = ApprovalDecision(approval_id=pending[0].approval_id, status=ApprovalStatus.APPROVED, decided_by="human:huyen")
        await rig.approvals.decide(d)
        await signal_approval(temporal_env.client, task.task_id, d)
        verdict = await asyncio.wait_for(handle.result(), 60)
    assert "db.migrate" in h.fw.executed
    assert verdict.decision is VerdictDecision.PASS  # db.migrate trả TEST_RESULT pass (fake worker)
    rec = (await rig.evidence.list_for_task("zeusvn", task.task_id))[0]
    assert rec.human_intervention is True


async def test_approval_rejected_never_executes(temporal_env, rig):
    task = make_task("Chạy migration cho bảng đơn hàng", family=TaskFamily.DATABASE)
    async with Harness(temporal_env, rig) as h:
        handle = await h.start(task)
        await h.wait_phase(handle, "AWAITING_APPROVAL")
        pending = (await rig.approvals.list("zeusvn", ApprovalStatus.PENDING))[0]
        d = ApprovalDecision(approval_id=pending.approval_id, status=ApprovalStatus.REJECTED, decided_by="human:huyen", comment="chưa")
        await rig.approvals.decide(d)
        await signal_approval(temporal_env.client, task.task_id, d)
        verdict = await asyncio.wait_for(handle.result(), 60)
    assert verdict.decision is VerdictDecision.FAIL and "từ chối" in verdict.reasons[0] and h.fw.seen == []
    assert (await rig.store.get_task("zeusvn", task.task_id)).status is TaskStatus.CANCELLED


async def test_approval_timeout_expires(temporal_env, rig):
    task = make_task("Chạy migration cho bảng đơn hàng", family=TaskFamily.DATABASE)
    async with Harness(temporal_env, rig) as h:
        verdict = await h.run(task, approval_timeout_s=1)
    assert verdict.decision is VerdictDecision.NEEDS_HUMAN and "EXPIRED" in verdict.reasons[0] and h.fw.seen == []
    assert [a.status for a in await rig.approvals.list("zeusvn")] == [ApprovalStatus.EXPIRED]
    assert (await rig.store.get_task("zeusvn", task.task_id)).status is TaskStatus.CANCELLED


async def test_approval_decided_in_store_but_signal_lost_is_reconciled(temporal_env, rig):
    task = make_task("Chạy migration cho bảng đơn hàng", family=TaskFamily.DATABASE)
    async with Harness(temporal_env, rig) as h:
        handle = await h.start(task, approval_timeout_s=2)
        await h.wait_phase(handle, "AWAITING_APPROVAL")
        pending = (await rig.approvals.list("zeusvn", ApprovalStatus.PENDING))[0]
        await rig.approvals.decide(ApprovalDecision(approval_id=pending.approval_id, status=ApprovalStatus.APPROVED, decided_by="human:h"))  # không signal
        verdict = await asyncio.wait_for(handle.result(), 60)
    assert verdict.decision is VerdictDecision.PASS and "db.migrate" in h.fw.executed


async def test_parallel_dag_nodes_dispatch_concurrently(temporal_env, models_cfg, policy_cfg):
    from zeus.contracts.models import TaskGraph, TaskNode, TypedAction, Plan

    rig = await make_rig(models_cfg, policy_cfg)

    class ForkPlanner:
        async def plan(self, task, context):
            def n(i, deps=(), name="test.run"):
                return TaskNode(node_id=i, title=i, depends_on=list(deps), acceptance=["ok"], required_capabilities=["python"],
                                action=TypedAction(name=name, tenant_id=task.tenant_id, task_id=task.task_id, args={}))
            g = TaskGraph(task_id=task.task_id, nodes=[n("a"), n("b"), n("c", ["a", "b"])])
            return Plan(task_id=task.task_id, graph=g, planner="playbook-1")

    rig.deps.planner = ForkPlanner()
    hold = asyncio.Event()
    task = make_task()
    async with Harness(temporal_env, rig, hold=hold) as h:
        handle = await h.start(task)
        for _ in range(100):  # a và b phải cùng được xếp vào queue khi cả hai đang bị giữ (song song)
            if len(rig.queue.owner) >= 2:
                break
            await asyncio.sleep(0.1)
        assert len(rig.queue.owner) == 2 and "c" not in {a.node_id for q in rig.queue.queues.values() for a in q}
        st = await handle.query(T.QUERY_STATUS)
        assert st["nodes"]["a"] == "RUNNING" and st["nodes"]["b"] == "RUNNING" and "c" not in st["nodes"]
        hold.set()
        verdict = await asyncio.wait_for(handle.result(), 60)
    assert verdict.decision is VerdictDecision.PASS and len(h.fw.executed) == 3


async def test_saga_compensation_rolls_back_succeeded_nodes_on_failure(temporal_env, rig):
    task = make_task()
    async with Harness(temporal_env, rig, fail_actions={"test.run"}) as h:
        verdict = await h.run(task)
    # implement thành công (code.apply_patch) -> test thất bại -> hoàn tác bằng code.revert_patch
    assert h.fw.executed == ["code.apply_patch", "test.run", "code.revert_patch"]
    assert verdict.decision is VerdictDecision.FAIL and verdict.outcome is Outcome.VERIFIED_FAILURE
    rec = (await rig.evidence.list_for_task("zeusvn", task.task_id))[0]
    assert rec.rollback_performed and rec.final_outcome is Outcome.VERIFIED_FAILURE
    assert (await rig.store.get_task("zeusvn", task.task_id)).status is TaskStatus.ROLLED_BACK
    assert any(r.result.ok for r in rig.queue.results.values() if r.assignment_id.endswith("_a1"))


async def test_cancel_signal_cancels_running_and_converges_to_verdict(temporal_env, rig):
    task = make_task()
    hold = asyncio.Event()  # worker không bao giờ trả kết quả
    async with Harness(temporal_env, rig, hold=hold) as h:
        handle = await h.start(task)
        for _ in range(100):
            if rig.queue.owner:
                break
            await asyncio.sleep(0.1)
        assert rig.queue.owner
        await signal_cancel(temporal_env.client, task.task_id, "người dùng huỷ")
        verdict = await asyncio.wait_for(handle.result(), 60)
        hold.set()
    assert verdict.decision is VerdictDecision.FAIL and "huỷ" in " ".join(verdict.reasons)
    assert (await rig.store.get_task("zeusvn", task.task_id)).status is TaskStatus.CANCELLED
    assert set(rig.queue.cancelled) and all(a.startswith("asg_") for a in rig.queue.cancelled)


async def test_no_worker_available_queues_then_runs_when_worker_appears(temporal_env, models_cfg, policy_cfg):
    rig = await make_rig(models_cfg, policy_cfg, register_worker=False)
    task = make_task()
    async with Harness(temporal_env, rig) as h:
        handle = await h.start(task, retry_initial_s=0.2, max_activity_attempts=6)
        await asyncio.sleep(1.0)  # task không mất: activity retry có backoff
        assert not rig.queue.owner and (await handle.describe()).status is WorkflowExecutionStatus.RUNNING
        from zeus.contracts.models import WorkerHeartbeat

        await rig.workers.register(make_worker())
        await rig.workers.heartbeat(WorkerHeartbeat(worker_id="w1", ram_available_mb=4096))
        verdict = await asyncio.wait_for(handle.result(), 60)
    assert verdict.decision is VerdictDecision.PASS


async def test_scheduled_task_workflow_creates_new_task_each_run(temporal_env, rig):
    template = make_task()
    async with Harness(temporal_env, rig) as h:
        sid = f"sched-{uuid.uuid4().hex[:6]}"
        handle = await create_schedule(temporal_env.client, sid, template, timedelta(hours=1), task_queue=h.queue, paused=True)
        await handle.trigger()
        for _ in range(200):
            await asyncio.sleep(0.1)
            if rig.outcomes.dataset:
                break
        await handle.delete()
    assert rig.outcomes.dataset and rig.outcomes.dataset[0].outcome is Outcome.VERIFIED_SUCCESS
    tasks = await rig.store.list_tasks("zeusvn")
    assert len(tasks) == 1 and tasks[0].task_id != template.task_id and tasks[0].status is TaskStatus.SUCCEEDED


def render_span_tree(rows) -> str:
    by_parent: dict = {}
    for r in rows:
        by_parent.setdefault(r["parent_span_id"], []).append(r)
    out: list[str] = []

    def walk(parent, depth):
        for r in sorted(by_parent.get(parent, []), key=lambda x: x["start_time_unix_nano"]):
            dur = (r["end_time_unix_nano"] - r["start_time_unix_nano"]) / 1e6
            out.append(f"{'  ' * depth}{r['name']}  [{r['status_code']}] {dur:.1f}ms")
            walk(r["span_id"], depth + 1)

    walk(None, 0)
    return "\n".join(out)


@pytest.mark.pg
async def test_end_to_end_on_postgres_stores_with_trace_span_tree(temporal_env, pg_dsn, models_cfg, policy_cfg, capsys):
    """Event -> Event Gateway (PG idempotency) -> TaskWorkflow (Temporal) -> approval (PG) -> dispatch -> judge; trace_spans có cây span."""
    import psycopg

    from zeus.contracts.models import Channel, Event, ChannelIdentity
    from zeus.gateway.gateway import EventGateway
    from zeus.gateway.store import PgControlStore
    from zeus.obs import SpanRecorder
    from zeus.policy.approvals import PgApprovalStore
    from zeus.policy.gateway import PgAudit
    from zeus.storage import apply_migrations
    from zeus.storage.spans import PgSpanExporter
    from pathlib import Path

    apply_migrations(pg_dsn, Path(__file__).resolve().parents[2] / "migrations")
    rig = await make_rig(models_cfg, policy_cfg, store=PgControlStore(pg_dsn), approvals=PgApprovalStore(pg_dsn), audit=PgAudit(pg_dsn),
                         spans=SpanRecorder([PgSpanExporter(pg_dsn)]))
    gw = EventGateway(rig.store, rig.deps.intent, rig.deps.risk)
    res = await gw.ingest(Event(channel=Channel.WORKBENCH, text="Chạy migration database cho bảng đơn hàng", external_id="e2e-1", sender=ChannelIdentity(channel_user_id="op"), signature_verified=True, untrusted=False))
    assert not res.duplicate and res.task.family is TaskFamily.DATABASE
    async with Harness(temporal_env, rig) as h:
        handle = await h.start(res.task)
        await h.wait_phase(handle, "AWAITING_APPROVAL")
        pending = await rig.approvals.list("zeusvn", ApprovalStatus.PENDING)
        d = ApprovalDecision(approval_id=pending[0].approval_id, status=ApprovalStatus.APPROVED, decided_by="human:huyen")
        await rig.approvals.decide(d)
        await signal_approval(temporal_env.client, res.task.task_id, d)
        verdict = await asyncio.wait_for(handle.result(), 60)
    assert verdict.decision is VerdictDecision.PASS
    assert (await rig.store.get_task("zeusvn", res.task.task_id)).status is TaskStatus.SUCCEEDED
    with psycopg.connect(pg_dsn, row_factory=psycopg.rows.dict_row) as conn:
        rows = conn.execute("SELECT * FROM trace_spans WHERE task_id=%s", (res.task.task_id,)).fetchall()
        audits = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE task_id=%s", (res.task.task_id,)).fetchall()]
        nodes = conn.execute("SELECT node_id, status FROM task_nodes WHERE task_id=%s ORDER BY node_id", (res.task.task_id,)).fetchall()
    names = {r["name"] for r in rows}
    assert {"task.lifecycle", "control.classify_and_assess", "control.plan", "control.critique", "control.prepare_approvals", "control.judge_and_record"} <= names
    root = [r for r in rows if r["name"] == "task.lifecycle"]
    assert len(root) == 1 and root[0]["parent_span_id"] is None and all(r["parent_span_id"] == root[0]["span_id"] for r in rows if r is not root[0])
    assert len({r["trace_id"] for r in rows}) == 1 and "approval.requested" not in audits  # approval do workflow tạo, không qua gateway
    assert {n["node_id"]: n["status"] for n in nodes}["migrate"] == "SUCCEEDED"
    with capsys.disabled():
        print("\n--- SPAN TREE (trace_spans) ---\n" + render_span_tree(rows))
