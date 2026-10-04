from __future__ import annotations

import uuid

import pytest
from temporalio.worker import Worker

from zeus.contracts.api import TASK_QUEUE_CONTROL
from zeus.contracts.models import Outcome, Task, TaskFamily, TraceContext, VerdictDecision
from zeus.obs import new_trace_id
from zeus.testing.temporal_smoke import SmokeWorkflow, smoke_execute, smoke_judge


@pytest.mark.temporal
@pytest.mark.cloud_exit_workflow
async def test_smoke_workflow_runs_on_local_temporal(temporal_env):
    client = temporal_env.client
    queue = f"{TASK_QUEUE_CONTROL}-smoke-{uuid.uuid4().hex[:8]}"
    task = Task(family=TaskFamily.BACKEND, goal="smoke", trace=TraceContext(trace_id=new_trace_id()))
    async with Worker(client, task_queue=queue, workflows=[SmokeWorkflow], activities=[smoke_execute, smoke_judge]):
        verdict = await client.execute_workflow(SmokeWorkflow.run, task, id=f"smoke-{task.task_id}", task_queue=queue)
    assert verdict.decision is VerdictDecision.PASS
    assert verdict.outcome is Outcome.VERIFIED_SUCCESS
    assert verdict.task_id == task.task_id and verdict.evidence_ids
