"""Async activity completion thật trên Temporal (ADR-011): activity chờ -> worker POST result -> workflow nhận ActionResult."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio import workflow
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from zeus.contracts.api import TASK_QUEUE_DISPATCH
    from zeus.contracts.models import ActionResult, Task, TaskFamily, TaskNode, TypedAction
    from zeus.workers.dispatch import ACTIVITY_DISPATCH, DispatchActivities, DispatchRequest, TemporalCompleter
    from zeus_worker.agent import WorkerAgent
    from zeus_worker.client import WorkerApiClient
    from zeus_worker.config import WorkerConfig

pytestmark = [pytest.mark.pg, pytest.mark.temporal, pytest.mark.cloud_exit_worker_control]


@workflow.defn
class DispatchOnce:
    @workflow.run
    async def run(self, req: DispatchRequest) -> ActionResult:
        return await workflow.execute_activity(
            ACTIVITY_DISPATCH, req, result_type=ActionResult, start_to_close_timeout=timedelta(minutes=5)
        )


def request(name: str, timeout_s: int = 30, **args) -> DispatchRequest:
    task = Task(family=TaskFamily.BACKEND, goal="g")
    node = TaskNode(node_id="n1", title="t", required_capabilities=["python"])
    return DispatchRequest(task=task, node=node, action=TypedAction(name=name, args=args, task_id=task.task_id), timeout_s=timeout_s)


async def start_agent(stack, tmp_path: Path, wid="w-tmp"):
    token = await stack.registry.issue_token(wid)
    cfg = WorkerConfig(server_url="http://zeus", worker_id=wid, work_dir=tmp_path, extra_capabilities=["python"], heartbeat_interval_s=0.2, poll_wait_s=1)
    client = WorkerApiClient("http://zeus", token, transport=stack.transport())
    agent = WorkerAgent(cfg, client)
    stop = asyncio.Event()
    return agent, client, stop, asyncio.create_task(agent.run_forever(stop))


async def test_async_completion_workflow_receives_worker_result(stack, temporal_env, tmp_path):
    completer = TemporalCompleter(temporal_env.client)
    stack.services.completer = completer
    stack.dispatcher.completer = completer
    acts = DispatchActivities(stack.dispatcher)
    agent, client, stop, loop_task = await start_agent(stack, tmp_path)
    try:
        async with Worker(temporal_env.client, task_queue=TASK_QUEUE_DISPATCH, workflows=[DispatchOnce], activities=[acts.dispatch_action]):
            res = await asyncio.wait_for(
                temporal_env.client.execute_workflow(
                    DispatchOnce.run, request("noop.echo", greeting="hi"), id=f"wf-{uuid.uuid4().hex}", task_queue=TASK_QUEUE_DISPATCH
                ),
                60,
            )
    finally:
        stop.set()
        await asyncio.wait_for(loop_task, 10)
        await client.aclose()
    assert isinstance(res, ActionResult) and res.ok and res.output == {"echo": {"greeting": "hi"}} and res.executed_by == "w-tmp"


async def test_cancel_before_worker_completes_workflow_with_cancelled_result(stack, temporal_env):
    completer = TemporalCompleter(temporal_env.client)
    stack.services.completer = completer
    stack.dispatcher.completer = completer
    acts = DispatchActivities(stack.dispatcher)
    async with Worker(temporal_env.client, task_queue=TASK_QUEUE_DISPATCH, workflows=[DispatchOnce], activities=[acts.dispatch_action]):
        handle = await temporal_env.client.start_workflow(
            DispatchOnce.run, request("noop.echo"), id=f"wf-{uuid.uuid4().hex}", task_queue=TASK_QUEUE_DISPATCH
        )
        aid = None
        for _ in range(100):  # chờ assignment xuất hiện (không có worker nào => chờ scheduler, worker_id NULL)
            pending = await stack.queue.unassigned()
            if pending:
                aid = pending[0][0].assignment_id
                break
            await asyncio.sleep(0.1)
        assert aid
        decision_rows = await stack.store.success_stats(TaskFamily.BACKEND)
        assert decision_rows == {}
        # không worker nào đủ điều kiện => xếp hàng; huỷ => activity hoàn thành với ok=False
        assert await stack.dispatcher.cancel(aid) == "CANCELLED"
        res = await asyncio.wait_for(handle.result(), 30)
    assert res.ok is False and res.error == "cancelled"
