"""Dispatch: scheduler + queue + Temporal async activity completion (ADR-011).

Phía control: activity ``dispatch_action`` chọn worker, enqueue kèm task_token rồi raise_complete_async.
Phía Worker API: khi worker POST kết quả, ``TemporalCompleter`` hoàn thành activity bằng task_token.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Protocol

from temporalio import activity
from temporalio.client import Client

from zeus.contracts.api import TaskAssignment
from zeus.contracts.models import (
    ActionResult,
    RiskLevel,
    Task,
    TaskFamily,
    TaskNode,
    TraceContext,
    TypedAction,
    ZeusModel,
    new_id,
    utcnow,
)
from zeus.workers.config import WorkersConfig
from zeus.workers.queue import PgAssignmentQueue
from zeus.workers.registry import PgWorkerRegistry
from zeus.workers.scheduler import DeterministicScheduler, PgScheduleStore

log = logging.getLogger("zeus.workers.dispatch")

ACTIVITY_DISPATCH = "zeus.dispatch_action"


class DispatchRequest(ZeusModel):
    task: Task
    node: TaskNode
    action: TypedAction
    timeout_s: int = 600


class ActivityCompleter(Protocol):
    async def complete(self, task_token: bytes, result: ActionResult) -> None: ...


class TemporalCompleter:
    """Hoàn thành async activity bằng task_token (client dùng pydantic data converter)."""

    def __init__(self, client: Client) -> None:
        self.client = client

    async def complete(self, task_token: bytes, result: ActionResult) -> None:
        await self.client.get_async_activity_handle(task_token=task_token).complete(result)


def cancelled_result(action_id: str, worker_id: str | None, why: str = "cancelled") -> ActionResult:
    return ActionResult(action_id=action_id, ok=False, error=why, executed_by=worker_id)


class Dispatcher:
    def __init__(
        self,
        registry: PgWorkerRegistry,
        queue: PgAssignmentQueue,
        scheduler: DeterministicScheduler,
        store: PgScheduleStore,
        config: WorkersConfig,
        completer: ActivityCompleter | None = None,
    ) -> None:
        self.registry, self.queue, self.scheduler, self.store, self.config = registry, queue, scheduler, store, config
        self.completer = completer
        self.stuck_grace_s = 15.0  # chỉ vớt assignment đã kết thúc quá ngần này giây mà activity chưa hoàn thành

    async def _decide(self, task: Task, node: TaskNode):
        workers = await self.registry.list()
        decision = await self.scheduler.schedule(task, node, workers, await self.registry.heartbeats())
        await self.store.save(decision)
        return decision

    async def dispatch(self, req: DispatchRequest, task_token: bytes | None = None) -> TaskAssignment:
        decision = await self._decide(req.task, req.node)
        trace = req.task.trace or TraceContext(trace_id=uuid.uuid4().hex, tenant_id=req.task.tenant_id, task_id=req.task.task_id)
        assignment = TaskAssignment(
            assignment_id=new_id("asg"), task_id=req.task.task_id, node_id=req.node.node_id, action=req.action,
            timeout_s=req.timeout_s, lease_expires_at=utcnow() + timedelta(seconds=self.config.lease_ttl_s),
            trace=trace, schedule_decision_id=decision.decision_id,
        )
        ctx = {
            "family": req.task.family.value, "urgency": req.task.urgency, "risk": req.node.risk.value,
            "required_capabilities": req.node.required_capabilities,
        }
        await self.queue.enqueue(assignment, decision.worker_id, task_token, ctx)  # worker_id None => chờ scheduler
        return assignment

    async def reschedule_unassigned(self) -> int:
        """Chọn lại worker cho assignment đang chờ (chưa có worker / worker cũ OFFLINE) từ sched_context đã lưu."""
        n = 0
        for a, ctx in await self.queue.unassigned():
            task = Task(
                task_id=a.task_id, tenant_id=a.trace.tenant_id, family=TaskFamily(ctx.get("family", "general")),
                goal=a.action.name, urgency=ctx.get("urgency", 1),
            )
            node = TaskNode(
                node_id=a.node_id or "n", title=a.action.name, risk=RiskLevel(ctx.get("risk", "R0")),
                required_capabilities=list(ctx.get("required_capabilities", [])),
            )
            decision = await self._decide(task, node)
            if decision.worker_id and await self.queue.assign_worker(a.assignment_id, decision.worker_id, decision.decision_id):
                n += 1
        return n

    async def cancel(self, assignment_id: str) -> str:
        state, token = await self.queue.cancel_ex(assignment_id)
        if token and self.completer:
            await self.completer.complete(token, cancelled_result(assignment_id, None))
            await self.queue.mark_activity_completed(assignment_id)
        return state

    async def sweep(self, now: datetime | None = None) -> dict[str, list[str]]:
        """Một vòng bảo trì: worker mất tín hiệu => OFFLINE; lease hết hạn => trả hàng đợi; vượt số lần => FAILED."""
        now = now or utcnow()
        stale = await self.registry.mark_stale(now, self.config.stale_ttl_s)
        exp = await self.queue.requeue_expired(now)
        for aid, token in exp.failed:
            if token and self.completer:
                # status đã FAILED trong DB: completer lỗi thì vòng ``complete_stuck`` bên dưới sẽ thử lại, token không mất (R5)
                try:
                    await self.completer.complete(token, cancelled_result(aid, None, "lease expired: quá số lần thử"))
                    await self.queue.mark_activity_completed(aid)
                except Exception:  # noqa: BLE001
                    log.exception("hoàn thành activity cho %s lỗi (sẽ thử lại)", aid)
        await self.reschedule_unassigned()
        stuck = await self.complete_stuck()
        return {"offline": stale, "requeued": exp.requeued, "released": exp.released, "failed": [a for a, _ in exp.failed], "completed": stuck}

    async def complete_stuck(self) -> list[str]:
        """Hoàn thành activity cho assignment đã kết thúc mà worker/API/sweep không hoàn thành được (R5): đọc kết quả đã lưu
        rồi dùng task_token đã lưu. Lặp lại an toàn (activity đã xong => completer nuốt NOT_FOUND)."""
        if not self.completer:
            return []
        done: list[str] = []
        for aid, status, token in await self.queue.uncompleted(self.stuck_grace_s):
            try:
                res = await self.queue.result_of(aid) if status == "COMPLETED" else None
                full_fn = getattr(self.completer, "complete_assignment", None)
                if res is not None and full_fn is not None:
                    await full_fn(token, res)
                elif res is not None:
                    await self.completer.complete(token, res.result)
                else:
                    await self.completer.complete(token, cancelled_result(aid, None, f"assignment {status.lower()}"))
                await self.queue.mark_activity_completed(aid)
                done.append(aid)
            except Exception:  # noqa: BLE001
                log.exception("hoàn thành activity cho %s lỗi (sẽ thử lại)", aid)
        return done


class DispatchActivities:
    """Đăng ký vào Temporal Worker trên TASK_QUEUE_DISPATCH: ``activities=[DispatchActivities(d).dispatch_action]``."""

    def __init__(self, dispatcher: Dispatcher) -> None:
        self.dispatcher = dispatcher

    @activity.defn(name=ACTIVITY_DISPATCH)
    async def dispatch_action(self, req: DispatchRequest) -> ActionResult:
        token = activity.info().task_token
        await self.dispatcher.dispatch(req, token)
        activity.raise_complete_async()


__all__ = ["ACTIVITY_DISPATCH", "ActivityCompleter", "DispatchActivities", "DispatchRequest", "Dispatcher", "TemporalCompleter"]
