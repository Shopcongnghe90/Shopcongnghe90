"""Client helpers: kết nối Temporal, build worker, start workflow idempotent (id = task id), signal, schedule."""

from __future__ import annotations

from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleSpec,
    ScheduleState,
    WorkflowHandle,
)
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from zeus.contracts.api import TASK_QUEUE_CONTROL
from zeus.contracts.models import ApprovalDecision, Event, Task
from zeus.orchestration import types as T
from zeus.orchestration.activities import ControlActivities, ControlDeps
from zeus.orchestration.workflow import ScheduledTaskWorkflow, TaskWorkflow


async def connect(address: str = "127.0.0.1:7233", namespace: str = "default") -> Client:
    return await Client.connect(address, namespace=namespace, data_converter=pydantic_data_converter)


def build_worker(client: Client, deps: ControlDeps, task_queue: str = TASK_QUEUE_CONTROL) -> Worker:
    return Worker(client, task_queue=task_queue, workflows=[TaskWorkflow, ScheduledTaskWorkflow], activities=ControlActivities(deps).all())


async def start_task_workflow(
    client: Client, task: Task, event: Event | None = None, *, task_queue: str = TASK_QUEUE_CONTROL, **opts: object
) -> WorkflowHandle:
    """Workflow id = task id. Gọi lại với cùng task => trả handle của workflow đang/đã chạy (không tạo bản thứ hai)."""
    inp = T.TaskWorkflowInput(task=task, event=event, **opts)  # type: ignore[arg-type]
    try:
        return await client.start_workflow(
            TaskWorkflow.run, inp, id=task.task_id, task_queue=task_queue, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE
        )
    except WorkflowAlreadyStartedError:
        return client.get_workflow_handle(task.task_id)


async def signal_approval(client: Client, task_id: str, decision: ApprovalDecision) -> None:
    await client.get_workflow_handle(task_id).signal(T.SIGNAL_APPROVAL, decision)


async def signal_cancel(client: Client, task_id: str, reason: str = "") -> None:
    await client.get_workflow_handle(task_id).signal(T.SIGNAL_CANCEL, reason)


async def complete_assignment(client: Client, task_token: bytes, result: object) -> None:
    """Cầu nối cho Worker API (C): hoàn thành activity dispatch bất đồng bộ bằng AssignmentResult."""
    await client.get_async_activity_handle(task_token=task_token).complete(result)


async def create_schedule(client: Client, schedule_id: str, template: Task, every: timedelta, *, task_queue: str = TASK_QUEUE_CONTROL, paused: bool = False):  # noqa: ANN201
    """Lịch lặp: mỗi lần chạy sinh Task mới (ScheduledTaskWorkflow). Trả ScheduleHandle."""
    return await client.create_schedule(
        schedule_id,
        Schedule(
            action=ScheduleActionStartWorkflow(ScheduledTaskWorkflow.run, template, id=f"{schedule_id}-run", task_queue=task_queue),
            spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=every)]),
            state=ScheduleState(paused=paused),
        ),
    )


__all__ = [
    "connect", "build_worker", "start_task_workflow", "signal_approval", "signal_cancel", "complete_assignment",
    "create_schedule",
]
