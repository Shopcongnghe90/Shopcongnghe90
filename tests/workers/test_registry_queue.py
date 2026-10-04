from __future__ import annotations

from datetime import timedelta

import pytest

from tests.workers.conftest import make_info
from zeus.contracts.api import AssignmentResult, CancelAck, TaskAssignment
from zeus.contracts.interfaces import AssignmentQueue, Scheduler, WorkerRegistry
from zeus.contracts.models import (
    ActionResult,
    TraceContext,
    TypedAction,
    WorkerHeartbeat,
    WorkerStatus,
    utcnow,
)
from zeus.workers.queue import PgAssignmentQueue
from zeus.workers.registry import PgWorkerRegistry
from zeus.workers.scheduler import DeterministicScheduler

pytestmark = pytest.mark.pg


def asg(aid: str, task="t1") -> TaskAssignment:
    return TaskAssignment(
        assignment_id=aid, task_id=task, node_id="n1", action=TypedAction(name="noop.echo", args={"x": 1}),
        lease_expires_at=utcnow() + timedelta(seconds=60), trace=TraceContext(trace_id="a" * 32),
    )


def res(aid: str, wid: str, ok=True) -> AssignmentResult:
    return AssignmentResult(assignment_id=aid, worker_id=wid, result=ActionResult(action_id="act_1", ok=ok))


def test_protocols_satisfied(stack):
    assert isinstance(stack.registry, WorkerRegistry)
    assert isinstance(stack.queue, AssignmentQueue)
    assert isinstance(stack.scheduler, Scheduler)


async def test_registry_roundtrip_tokens_and_stale(stack):
    reg: PgWorkerRegistry = stack.registry
    tok = await reg.issue_token("w1", ["zeusvn"])
    g = await reg.verify_token(tok)
    assert g and g.worker_id == "w1"
    assert await reg.verify_token("zwt_sai") is None and await reg.verify_token(None) is None
    # DB chỉ giữ hash
    import psycopg

    with psycopg.connect(stack.dsn) as c:
        rows = c.execute("SELECT token_hash FROM worker_tokens").fetchall()
    assert rows and all(tok not in r[0] and len(r[0]) == 64 for r in rows)

    info = make_info("w1", data_localities=["repo:x"])
    await reg.register(info)
    got = await reg.get("w1")
    assert got.capabilities == info.capabilities and got.inventory == info.inventory and got.data_localities == ["repo:x"]
    assert await reg.last_heartbeat("w1") is None
    hb = WorkerHeartbeat(worker_id="w1", cpu_available_pct=55.0, ram_available_mb=1000, queue_depth=2)
    await reg.heartbeat(hb)
    assert (await reg.last_heartbeat("w1")).cpu_available_pct == 55.0
    with pytest.raises(KeyError):
        await reg.heartbeat(WorkerHeartbeat(worker_id="ghost"))

    await reg.register(make_info("w2"))
    assert [w.worker_id for w in await reg.list()] == ["w1", "w2"]
    stale = await reg.mark_stale(utcnow() + timedelta(seconds=120), 60)
    assert stale == ["w1", "w2"]
    assert await reg.list(WorkerStatus.ONLINE) == []
    assert await reg.mark_stale(utcnow() + timedelta(seconds=120), 60) == []  # idempotent
    await reg.heartbeat(hb)  # sống lại
    assert (await reg.get("w1")).status is WorkerStatus.ONLINE
    assert await reg.revoke_tokens("w1") == 1 and await reg.verify_token(tok) is None


async def test_queue_lease_ownership_idempotent_complete(stack):
    q: PgAssignmentQueue = stack.queue
    await stack.registry.register(make_info("w1"))
    await stack.registry.register(make_info("w2"))
    await q.enqueue(asg("a1"), "w1", b"TOKEN1")
    with pytest.raises(ValueError):
        await q.enqueue(asg("a1"), "w1", b"x")
    assert await q.poll("w2") == []  # không phải của w2
    got = await q.poll("w1")
    assert [a.assignment_id for a in got] == ["a1"]
    assert await q.poll("w1") == []  # đã lease, không phát lại
    with pytest.raises(PermissionError):
        await q.complete(res("a1", "w2"))
    assert await q.complete(res("a1", "w1")) == b"TOKEN1"
    assert await q.complete(res("a1", "w1")) == b"TOKEN1"  # activity chưa được đánh dấu hoàn thành => trả lại token
    await q.mark_activity_completed("a1")
    assert await q.complete(res("a1", "w1")) is None  # idempotent
    assert (await q.status("a1"))["status"] == "COMPLETED"


async def test_queue_skip_locked_no_double_lease(stack):
    import asyncio

    q: PgAssignmentQueue = stack.queue
    await stack.registry.register(make_info("w1"))
    for i in range(6):
        await q.enqueue(asg(f"a{i}"), "w1")
    batches = await asyncio.gather(*[q.poll("w1", 2) for _ in range(4)])
    ids = [a.assignment_id for b in batches for a in b]
    assert sorted(ids) == [f"a{i}" for i in range(6)] and len(ids) == len(set(ids))


async def test_lease_expiry_requeues_and_offline_releases(stack):
    q: PgAssignmentQueue = stack.queue
    await stack.registry.register(make_info("w1"))
    await stack.registry.register(make_info("w2"))
    await q.enqueue(asg("a1"), "w1", b"T")
    await q.poll("w1")
    far = utcnow() + timedelta(seconds=3600)
    # lease hết hạn nhưng worker còn sống => quay lại hàng đợi của chính nó, attempt+1
    exp = await q.requeue_expired(far)
    assert exp.requeued == ["a1"] and exp.released == []
    s = await q.status("a1")
    assert s["status"] == "QUEUED" and s["attempt"] == 2 and s["worker_id"] == "w1"
    again = await q.poll("w1")
    assert again[0].attempt == 2
    # worker OFFLINE => assignment bị bỏ worker, scheduler chọn lại sang w2
    await stack.registry.heartbeat(WorkerHeartbeat(worker_id="w2"))
    await stack.registry.set_status("w1", WorkerStatus.OFFLINE)
    out = await stack.dispatcher.sweep(far + timedelta(seconds=1))
    assert out["released"] == ["a1"]
    assert (await q.status("a1"))["worker_id"] in ("w2", None)
    # w2 stale cũng bị sweep OFFLINE (không heartbeat trong far) => assignment chờ ở worker_id NULL
    s = await q.status("a1")
    if s["worker_id"] is None:
        await stack.registry.heartbeat(WorkerHeartbeat(worker_id="w2"))
        assert await stack.dispatcher.reschedule_unassigned() == 1
    assert (await q.status("a1"))["worker_id"] == "w2"
    assert [a.assignment_id for a in await q.poll("w2")] == ["a1"]


async def test_attempts_exhausted_fails_with_token(stack):
    q = PgAssignmentQueue(stack.dsn, lease_ttl_s=60, max_attempts=1)
    await stack.registry.register(make_info("w1"))
    await q.enqueue(asg("a1"), "w1", b"T")
    await q.poll("w1")
    exp = await q.requeue_expired(utcnow() + timedelta(seconds=3600))
    assert exp.failed == [("a1", b"T")] and (await q.status("a1"))["status"] == "FAILED"


async def test_heartbeat_extends_lease(stack):
    q: PgAssignmentQueue = stack.queue
    await stack.registry.register(make_info("w1"))
    await q.enqueue(asg("a1"), "w1")
    await q.poll("w1")
    assert await q.extend_leases("w1", ["a1", "nope"]) == 1
    assert await q.extend_leases("w2", ["a1"]) == 0
    assert (await q.requeue_expired(utcnow() + timedelta(seconds=30))).requeued == []  # lease mới ~60s


async def test_cancel_paths(stack):
    q: PgAssignmentQueue = stack.queue
    await stack.registry.register(make_info("w1"))
    await q.enqueue(asg("q1"), "w1", b"TQ")
    await q.enqueue(asg("l1"), "w1", b"TL")
    assert await q.cancel_ex("q1") == ("CANCELLED", b"TQ")  # chưa lease: huỷ ngay
    assert [a.assignment_id for a in await q.poll("w1")] == ["l1"]
    assert await q.pending_cancellations("w1") == []
    await q.cancel("l1")  # đang chạy: lan tới worker qua heartbeat
    assert await q.pending_cancellations("w1") == ["l1"]
    with pytest.raises(PermissionError):
        await q.ack_cancel(CancelAck(assignment_id="l1", worker_id="w9", cancelled=True))
    assert await q.ack_cancel(CancelAck(assignment_id="l1", worker_id="w1", cancelled=True)) == b"TL"
    assert await q.pending_cancellations("w1") == []
    assert await q.complete(res("l1", "w1")) is None  # kết quả muộn sau cancel không hoàn thành activity
