"""Cloud Exit WORKER_CONTROL: register -> heartbeat -> schedule -> poll -> execute -> result qua Worker API thật,
Postgres thật, thin worker chạy trong tiến trình test; không có Claude Cloud / mạng ngoài."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta

import psycopg
import pytest

from zeus.contracts.models import Outcome, Task, TaskFamily, TaskNode, TypedAction, WorkerStatus, utcnow
from zeus.workers.dispatch import DispatchRequest
from zeus_worker.agent import WorkerAgent
from zeus_worker.client import WorkerApiClient
from zeus_worker.config import WorkerConfig

pytestmark = [pytest.mark.pg, pytest.mark.cloud_exit_worker_control]


async def boot(stack, tmp_path, wid, caps):
    token = await stack.registry.issue_token(wid)
    cfg = WorkerConfig(server_url="http://zeus", worker_id=wid, work_dir=tmp_path, extra_capabilities=caps, file_roots=[tmp_path])
    client = WorkerApiClient("http://zeus", token, transport=stack.transport())
    agent = WorkerAgent(cfg, client)
    await agent.register()
    await agent.heartbeat_once()
    return agent, client


async def test_worker_control_full_cycle(stack, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CLOUD_AVAILABLE", "false")
    for k in [k for k in os.environ if k.startswith("CLAUDE_CODE_")]:
        monkeypatch.delenv(k)
    (tmp_path / "f.txt").write_text("zeus")
    code, client_code = await boot(stack, tmp_path, "w-code", ["odoo_addon_dev"])
    gpu, client_gpu = await boot(stack, tmp_path, "w-gpu", ["gpu_llm"])
    assert {w.worker_id for w in await stack.registry.list(WorkerStatus.ONLINE)} == {"w-code", "w-gpu"}

    # schedule: chỉ w-code đủ capability; w-gpu bị loại và có lý do trong ScheduleDecision
    task = Task(family=TaskFamily.BACKEND, goal="checksum file", urgency=2)
    node = TaskNode(node_id="n1", title="checksum", required_capabilities=["odoo_addon_dev"])
    action = TypedAction(name="file.checksum", args={"path": str(tmp_path / "f.txt")}, task_id=task.task_id)
    asg = await stack.dispatcher.dispatch(DispatchRequest(task=task, node=node, action=action), b"TOKEN-CE")
    d = await stack.store.get(asg.schedule_decision_id)
    assert d.worker_id == "w-code" and d.features is not None
    assert {c.worker_id: c.rejected_reason is None for c in d.candidates} == {"w-code": True, "w-gpu": False}

    # poll: worker khác không thấy; worker đúng nhận được; thực thi; kết quả về Worker API
    assert await gpu.poll_once(wait_s=0) == []
    got = await code.poll_once(wait_s=0)
    assert [a.assignment_id for a in got] == [asg.assignment_id]
    await asyncio.wait_for(code.wait_idle(), 20)

    token, result = stack.completer.calls[0]
    assert token == b"TOKEN-CE" and result.ok and result.executed_by == "w-code"
    import hashlib

    assert result.output["sha256"] == hashlib.sha256(b"zeus").hexdigest()
    with psycopg.connect(stack.dsn) as c:
        st, ac = c.execute("SELECT status, activity_completed FROM assignments WHERE assignment_id=%s", (asg.assignment_id,)).fetchone()
        nlogs = c.execute("SELECT count(*) FROM worker_artifacts WHERE assignment_id=%s AND kind='log'", (asg.assignment_id,)).fetchone()[0]
    assert (st, ac, nlogs) == ("COMPLETED", True, 1)

    # nhãn học: outcome được ghi vào ScheduleDecision
    await stack.store.record_outcome(d.decision_id, Outcome.VERIFIED_SUCCESS)
    assert (await stack.store.success_stats(TaskFamily.BACKEND)) == {"w-code": (1, 1)}

    # fault-injection: w-code im lặng -> OFFLINE; việc đang lease được trả lại và chuyển sang worker khác khi có
    long = TypedAction(name="noop.echo", args={"delay_s": 60}, task_id=task.task_id)
    asg2 = await stack.dispatcher.dispatch(DispatchRequest(task=task, node=node, action=long), b"TOKEN-2")
    await code.poll_once(wait_s=0)
    code_task = code.running[asg2.assignment_id]
    assert (await stack.queue.status(asg2.assignment_id))["status"] == "LEASED"
    out = await stack.dispatcher.sweep(utcnow() + timedelta(hours=1))
    assert set(out["offline"]) == {"w-code", "w-gpu"} and out["released"] == [asg2.assignment_id]
    assert (await stack.queue.status(asg2.assignment_id))["worker_id"] is None
    code_task.cancel()
    await asyncio.gather(code_task, return_exceptions=True)

    # w-code sống lại (heartbeat), scheduler giao lại, thực thi xong
    await code.heartbeat_once()
    assert (await stack.registry.get("w-code")).status is WorkerStatus.ONLINE
    assert await stack.dispatcher.reschedule_unassigned() == 1
    s = await stack.queue.status(asg2.assignment_id)
    assert s["worker_id"] == "w-code" and s["attempt"] == 2

    for cl in (client_code, client_gpu):
        await cl.aclose()
    print("SCHEDULE_DECISION_SAMPLE", json.dumps(d.model_dump(mode="json"), ensure_ascii=False)[:600])
