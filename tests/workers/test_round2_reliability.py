"""Hồi quy review round 2 (độ tin cậy, workstream C): R5 (mất kết quả worker -> control), cancel/queue helper."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest

from tests.workers.conftest import make_info
from tests.workers.test_registry_queue import asg
from tests.workers.test_zeus_worker import finished, make_agent, run_action
from zeus.contracts.api import AssignmentResult
from zeus.contracts.models import ActionResult, utcnow
from zeus.workers.queue import PgAssignmentQueue
from zeus_worker.config import WorkerConfig

pytestmark = pytest.mark.pg


async def test_r5_worker_retries_result_when_control_completion_fails(stack, tmp_path):
    """Completer lỗi lần đầu (502): worker phải gửi lại thay vì bỏ kết quả (trước đây: calls == [])."""
    agent, _ = await make_agent(stack, tmp_path, send_retry_base_s=0.05)
    stack.completer.fail_next = True
    a = await run_action(stack, agent, "noop.echo", msg="x")
    res = await finished(stack, agent, a)
    assert res is not None and res.ok and len(stack.completer.calls) == 1
    assert (await stack.queue.status(a.assignment_id))["activity_completed"] is True


async def test_r5_worker_does_not_retry_permanent_4xx(stack, tmp_path):
    agent, client = await make_agent(stack, tmp_path, send_retry_base_s=0.01)
    calls = {"n": 0}

    async def post(res):  # noqa: ANN001
        calls["n"] += 1
        raise httpx.HTTPStatusError("forbidden", request=httpx.Request("POST", "http://x"), response=httpx.Response(403))

    client.post_result = post  # type: ignore[method-assign]
    await run_action(stack, agent, "noop.echo", msg="x")
    await asyncio.wait_for(agent.wait_idle(), 20)
    assert calls["n"] == 1


async def test_r5_control_sweep_completes_activity_for_finished_assignment(stack):
    """Worker đã gửi kết quả (COMPLETED) nhưng completer lỗi và worker không gửi lại: sweep phải hoàn thành activity."""
    q: PgAssignmentQueue = stack.queue
    await stack.registry.register(make_info("w1"))
    await q.enqueue(asg("a1"), "w1", b"TOK1")
    await q.poll("w1")
    token = await q.complete(AssignmentResult(assignment_id="a1", worker_id="w1", result=ActionResult(action_id="act_1", ok=True, output={"v": 7})))
    assert token == b"TOK1" and stack.completer.calls == []  # không ai hoàn thành activity
    stack.dispatcher.stuck_grace_s = 0
    out = await stack.dispatcher.sweep()
    assert out["completed"] == ["a1"]
    assert [(t, r.ok, r.output) for t, r in stack.completer.calls] == [(b"TOK1", True, {"v": 7})]
    assert (await q.status("a1"))["activity_completed"] is True
    assert (await stack.dispatcher.sweep())["completed"] == []  # không hoàn thành lần hai


async def test_r5_failed_lease_with_failing_completer_is_not_lost(stack):
    """dispatch.sweep: FAILED đã commit rồi completer ném lỗi => trước đây token mất; nay sweep sau vớt lại."""
    q = PgAssignmentQueue(stack.dsn, lease_ttl_s=60, max_attempts=1)
    stack.dispatcher.queue = q
    stack.dispatcher.stuck_grace_s = 0
    await stack.registry.register(make_info("w1"))
    await q.enqueue(asg("a1"), "w1", b"TOK")
    await q.poll("w1")
    stack.completer.fail_next = True
    # lease hết hạn thật: kéo lease về quá khứ
    import psycopg

    with psycopg.connect(stack.dsn, autocommit=True) as c:
        c.execute("UPDATE assignments SET lease_expires_at = now() - interval '1 minute' WHERE assignment_id='a1'")
    out = await stack.dispatcher.sweep()  # không được ném; token không mất: cùng vòng sweep vớt lại qua complete_stuck
    assert out["failed"] == ["a1"] and out["completed"] == ["a1"] and [t for t, _ in stack.completer.calls] == [b"TOK"]
    assert (await stack.dispatcher.sweep())["completed"] == []


async def test_r2_queue_cancel_unknown_assignment_raises_keyerror_documented(stack):
    """Hợp đồng PgAssignmentQueue.cancel: assignment không tồn tại => KeyError (activity cancel_assignments phải bắt)."""
    with pytest.raises(KeyError):
        await stack.queue.cancel("asg_khong_ton_tai_a2")


async def test_r6_result_of_returns_stored_result(stack):
    q: PgAssignmentQueue = stack.queue
    await stack.registry.register(make_info("w1"))
    await q.enqueue(asg("a1"), "w1", b"T")
    await q.poll("w1")
    assert await q.result_of("a1") is None
    await q.complete(AssignmentResult(assignment_id="a1", worker_id="w1", result=ActionResult(action_id="act_1", ok=True, output={"k": 1})))
    got = await q.result_of("a1")
    assert got is not None and got.result.output == {"k": 1}
    _ = (utcnow(), timedelta(0), WorkerConfig)  # imports dùng chung
