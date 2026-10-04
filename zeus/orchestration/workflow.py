"""TaskWorkflow (Temporal): classify -> risk -> context -> plan -> critique -> (approval signal, timeout) ->
DAG song song theo phụ thuộc -> (saga compensation khi lỗi/huỷ) -> judge -> evidence + outcome -> Verdict.

Bất biến: workflow id = task id (idempotent); mọi gọi LLM/IO nằm trong activity; luôn hội tụ về Verdict.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from zeus.contracts.api import AssignmentResult
    from zeus.contracts.models import (
        ApprovalDecision,
        ApprovalStatus,
        ContextPacket,
        Critique,
        Plan,
        Severity,
        Task,
        TaskNode,
        TaskStatus,
        Verdict,
        VerdictDecision,
    )
    from zeus.orchestration import types as T

_NON_RETRYABLE = ["BudgetExceeded", "PolicyDenied", "ValidationError", "KeyError"]


@workflow.defn(name=T.WORKFLOW_NAME)
class TaskWorkflow:
    def __init__(self) -> None:
        self._decisions: dict[str, ApprovalDecision] = {}
        self._cancelled = False
        self._cancel_reason = ""
        self._phase = "PENDING"
        self._pending_approvals: list[str] = []
        self._node_status: dict[str, str] = {}

    # ------------------------------------------------------------------ signals / queries
    @workflow.signal(name=T.SIGNAL_APPROVAL)
    def approval_decision(self, decision: ApprovalDecision) -> None:
        self._decisions.setdefault(decision.approval_id, decision)

    @workflow.signal(name=T.SIGNAL_CANCEL)
    def cancel(self, reason: str = "") -> None:
        self._cancelled = True
        self._cancel_reason = reason or "cancelled"

    @workflow.query(name=T.QUERY_STATUS)
    def status(self) -> dict[str, Any]:
        return {"phase": self._phase, "pending_approvals": list(self._pending_approvals), "nodes": dict(self._node_status), "cancelled": self._cancelled}

    # ------------------------------------------------------------------ helpers
    def _retry(self, inp: T.TaskWorkflowInput, attempts: int | None = None) -> RetryPolicy:
        return RetryPolicy(
            initial_interval=timedelta(seconds=inp.retry_initial_s),
            backoff_coefficient=1.5,
            maximum_interval=timedelta(seconds=max(inp.retry_initial_s * 8, 1.0)),
            maximum_attempts=attempts or inp.max_activity_attempts,
            non_retryable_error_types=_NON_RETRYABLE,
        )

    async def _act(self, inp: T.TaskWorkflowInput, name: str, result_type: Any, *args: Any, timeout_s: int = 120, attempts: int | None = None) -> Any:
        return await workflow.execute_activity(
            name, args=list(args), result_type=result_type, start_to_close_timeout=timedelta(seconds=timeout_s), retry_policy=self._retry(inp, attempts)
        )

    async def _finish(self, inp: T.TaskWorkflowInput, task: Task, started_ms: int, **kw: Any) -> Verdict:
        self._phase = "JUDGING"
        out: T.JudgeOutput = await self._act(
            inp, T.ACT_JUDGE, T.JudgeOutput,
            T.JudgeRequest(task=task, started_at_ms=started_ms, finished_at_ms=int(workflow.now().timestamp() * 1000), **kw),
            timeout_s=120,
        )
        self._phase = "DONE"
        return out.verdict

    async def _abort(self, inp: T.TaskWorkflowInput, task: Task, started_ms: int, reason: str, decision: VerdictDecision = VerdictDecision.FAIL,
                     status: TaskStatus = TaskStatus.FAILED, **kw: Any) -> Verdict:
        return await self._finish(inp, task, started_ms, abort_reason=reason, abort_decision=decision, abort_status=status, **kw)

    # ------------------------------------------------------------------ run
    @workflow.run
    async def run(self, inp: T.TaskWorkflowInput) -> Verdict:
        started_ms = int(workflow.now().timestamp() * 1000)
        cls: T.ClassifyResult = await self._act(inp, T.ACT_CLASSIFY, T.ClassifyResult, inp, timeout_s=120)
        task = cls.task
        if cls.denied:
            return await self._abort(inp, task, started_ms, "Bị Risk Engine từ chối cứng (R3/deny): " + "; ".join(cls.reasons[-3:]))

        ctx: ContextPacket = await self._act(inp, T.ACT_CONTEXT, ContextPacket, task, timeout_s=60)
        plan: Plan = await self._act(inp, T.ACT_PLAN, Plan, task, ctx, timeout_s=180)
        crit: Critique = await self._act(inp, T.ACT_CRITIQUE, Critique, task, plan, ctx, timeout_s=180)
        feats = dict(plan.route.features) if plan.route else {}
        common = {"planner": plan.planner, "route_features": feats, "route_id": plan.route.route_id if plan.route else None}
        if crit.has_blocker:
            msgs = [i.message for i in crit.issues if i.severity is Severity.BLOCKER]
            return await self._abort(inp, task, started_ms, "Critic chặn kế hoạch: " + "; ".join(msgs), VerdictDecision.NEEDS_HUMAN, TaskStatus.FAILED, **common)
        if self._cancelled:
            return await self._finish(inp, task, started_ms, cancelled=True, notes=[self._cancel_reason], **common)

        # ---- approval gate
        bundle: T.ApprovalBundle = await self._act(inp, T.ACT_APPROVALS, T.ApprovalBundle, task, plan, inp.approval_timeout_s, timeout_s=60)
        if bundle.denied_reasons:
            return await self._abort(inp, task, started_ms, "Policy từ chối: " + "; ".join(bundle.denied_reasons), **common)
        human = False
        if bundle.requests:
            human = True
            ids = [r.approval_id for r in bundle.requests]
            await self._act(inp, T.ACT_STATUS, None, task.task_id, TaskStatus.AWAITING_APPROVAL, timeout_s=30)
            self._pending_approvals = ids
            self._phase = "AWAITING_APPROVAL"
            timeout = inp.approval_timeout_s if inp.approval_timeout_s is not None else 3600
            decided = lambda: self._cancelled or any(self._decisions.get(i) and self._decisions[i].status is ApprovalStatus.REJECTED for i in ids) or all(i in self._decisions for i in ids)  # noqa: E731
            states: dict[str, str] = {}
            try:
                await workflow.wait_condition(decided, timeout=timedelta(seconds=timeout))
                states = {i: self._decisions[i].status.value for i in ids if i in self._decisions}
            except asyncio.TimeoutError:
                rec: T.ApprovalStates = await self._act(inp, T.ACT_RECONCILE, T.ApprovalStates, task.tenant_id, ids, True, timeout_s=60)
                states = rec.states
            self._pending_approvals = []
            if self._cancelled:
                return await self._finish(inp, task, started_ms, cancelled=True, notes=[self._cancel_reason], human_intervention=True, **common)
            if any(s == ApprovalStatus.REJECTED.value for s in states.values()):
                return await self._abort(inp, task, started_ms, "Người duyệt từ chối", VerdictDecision.FAIL, TaskStatus.CANCELLED, human_intervention=True, **common)
            if not all(states.get(i) == ApprovalStatus.APPROVED.value for i in ids):
                return await self._abort(inp, task, started_ms, "Phê duyệt hết hạn (EXPIRED)", VerdictDecision.NEEDS_HUMAN, TaskStatus.CANCELLED, human_intervention=True, **common)

        # ---- DAG song song
        self._phase = "RUNNING"
        graph = plan.graph
        done: dict[str, T.NodeRun] = {}
        order: list[str] = []
        failed: dict[str, T.NodeRun | None] = {}
        errors: list[str] = []
        running: dict[str, asyncio.Task[Any]] = {}
        cancel_waiter = asyncio.ensure_future(workflow.wait_condition(lambda: self._cancelled))

        async def exec_node(node: TaskNode) -> AssignmentResult:
            self._node_status[node.node_id] = "RUNNING"
            attempts = min(T.MAX_NODE_ATTEMPTS, max(node.max_retries + 1, inp.max_activity_attempts))
            return await self._act(inp, T.ACT_RUN_NODE, AssignmentResult, task, node, timeout_s=inp.node_timeout_s, attempts=attempts)

        while True:
            if not self._cancelled and not failed:
                for nid in graph.ready_nodes(set(done)):
                    if nid not in running and nid not in failed:
                        running[nid] = asyncio.ensure_future(exec_node(graph.node(nid)))
            if not running:
                break
            await workflow.wait([*running.values(), cancel_waiter], return_when=asyncio.FIRST_COMPLETED)
            if self._cancelled:
                for t in running.values():
                    t.cancel()
                await asyncio.gather(*running.values(), return_exceptions=True)
                await self._act(inp, T.ACT_CANCEL_ASSIGN, int, task, list(running), timeout_s=60)
                for nid in running:
                    self._node_status[nid] = "CANCELLED"
                running.clear()
                break
            for nid in [n for n, t in running.items() if t.done()]:
                t = running.pop(nid)
                node = graph.node(nid)
                exc = t.exception()
                if exc is not None:
                    failed[nid] = None
                    errors.append(f"node {nid}: {type(exc).__name__}: {exc}")
                    self._node_status[nid] = "FAILED"
                    continue
                run = T.NodeRun(node=node, result=t.result())
                if run.result.result.ok:
                    done[nid] = run
                    order.append(nid)
                    self._node_status[nid] = "SUCCEEDED"
                else:
                    failed[nid] = run
                    errors.append(f"node {nid}: {run.result.result.error or 'thất bại'}")
                    self._node_status[nid] = "FAILED"
        cancel_waiter.cancel()

        # ---- saga compensation (đảo thứ tự hoàn thành)
        rolled = False
        if failed or self._cancelled:
            for nid in reversed(order):
                rb: TaskNode | None = await self._act(inp, T.ACT_ROLLBACK, TaskNode, T.RollbackRequest(task=task, node=done[nid].node, run=done[nid]), timeout_s=60)
                if rb is None:
                    continue
                try:
                    res: AssignmentResult = await self._act(inp, T.ACT_RUN_NODE, AssignmentResult, task, rb, timeout_s=inp.node_timeout_s)
                    ok = res.result.ok
                except Exception as exc:  # noqa: BLE001 - lỗi hoàn tác được ghi nhận, không dừng saga
                    errors.append(f"rollback {nid}: {type(exc).__name__}")
                    ok = False
                rolled = rolled or ok
                self._node_status[nid] = "ROLLED_BACK" if ok else self._node_status[nid]

        runs = [*done.values(), *[r for r in failed.values() if r is not None]]
        skipped = [n.node_id for n in graph.nodes if n.node_id not in done and n.node_id not in failed]
        notes = list(errors)
        if skipped and (failed or self._cancelled):
            notes.append("node không chạy: " + ", ".join(skipped))
        if self._cancelled:
            notes.append(self._cancel_reason)
        return await self._finish(inp, task, started_ms, results=runs, rollback_performed=rolled, human_intervention=human, cancelled=self._cancelled, notes=notes, **common)


@workflow.defn(name=T.SCHEDULED_WORKFLOW_NAME)
class ScheduledTaskWorkflow:
    """Được Temporal Schedule gọi theo lịch: mỗi lần tạo Task mới từ template rồi chạy TaskWorkflow như child."""

    @workflow.run
    async def run(self, template: Task) -> Verdict:
        tid = "tsk_" + workflow.uuid4().hex
        task = template.model_copy(update={"task_id": tid, "workflow_id": tid, "trace": None, "created_at": workflow.now(), "status": TaskStatus.PENDING})
        await workflow.execute_activity(T.ACT_REGISTER, task, result_type=Task, start_to_close_timeout=timedelta(seconds=30))
        return await workflow.execute_child_workflow(TaskWorkflow.run, T.TaskWorkflowInput(task=task), id=tid, result_type=Verdict)
