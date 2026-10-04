"""Activities Temporal phía control. Mọi gọi LLM / IO đều nằm ở đây; workflow chỉ điều phối (deterministic).

Dispatch node có capability tới worker: enqueue vào AssignmentQueue kèm task_token rồi ``raise_complete_async``;
Worker API (C) gọi ``AssignmentQueue.complete`` -> nhận token -> ``client.get_async_activity_handle(task_token=...).complete(AssignmentResult)``.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from zeus.contracts.api import AssignmentResult, TaskAssignment
from zeus.contracts.interfaces import (
    ApprovalRequired,
    BrainRetriever,
    Critic,
    EvidenceStore,
    IntentEngine,
    Judge,
    OutcomeRecorder,
    PolicyDenied,
    Planner,
    RiskEngine,
    Scheduler,
    WorkerRegistry,
    AssignmentQueue,
)
from zeus.contracts.models import (
    ActionResult,
    ApprovalRequest,
    ApprovalStatus,
    ContextPacket,
    Critique,
    Event,
    EventKind,
    Channel,
    EvidenceItem,
    EvidenceKind,
    EvidenceRecord,
    Outcome,
    Plan,
    PolicyEffect,
    ProviderKind,
    RetrievalQuery,
    RiskLevel,
    Task,
    TaskNode,
    TaskStatus,
    TraceContext,
    TypedAction,
    Verdict,
    VerdictDecision,
    WorkerStatus,
    utcnow,
)
from zeus.gateway.store import ControlStore
from zeus.intent.engine import DefaultIntentEngine
from zeus.obs import SpanRecorder, bind_trace, new_trace_id
from zeus.obs.spans import ATTR_TASK, ATTR_TENANT, ATTR_WORKFLOW, Span, SpanKind, SpanStatus
from zeus.orchestration import types as T
from zeus.planning.critic import planner_vendor
from zeus.planning.judge import DeterministicJudge, derive_outcome
from zeus.policy.engine import effective_risk
from zeus.policy.gateway import DefaultToolGateway
from zeus.risk.engine import is_denied

CONTROL_WORKER_ID = "control"


@dataclass
class ControlDeps:
    intent: IntentEngine
    risk: RiskEngine
    planner: Planner
    critic: Critic
    judge: Judge
    gateway: DefaultToolGateway
    approvals: Any  # ApprovalStore + expire()
    store: ControlStore
    evidence: EvidenceStore
    outcomes: OutcomeRecorder
    retriever: BrainRetriever | None = None
    scheduler: Scheduler | None = None
    workers: WorkerRegistry | None = None
    queue: AssignmentQueue | None = None
    spans: SpanRecorder | None = None
    max_assignment_attempts: int = T.MAX_NODE_ATTEMPTS


def _trace(task: Task) -> TraceContext:
    return task.trace or TraceContext(trace_id=new_trace_id(), tenant_id=task.tenant_id, task_id=task.task_id, workflow_id=task.workflow_id)


def root_span_id(trace_id: str) -> str:
    """Span gốc của cả vòng đời task (xuất ở cuối judge_and_record); mọi span activity là con của nó."""
    return hashlib.sha256(trace_id.encode()).hexdigest()[:16]


def assignment_id(task_id: str, node_id: str, attempt: int) -> str:
    return "asg_" + hashlib.sha256(f"{task_id}/{node_id}".encode()).hexdigest()[:20] + f"_a{attempt}"


def _accepts_context(queue: Any) -> bool:
    try:
        return "context" in inspect.signature(queue.enqueue).parameters
    except (TypeError, ValueError):
        return False


def _local_result(node_id: str, task: Task, res: ActionResult) -> AssignmentResult:
    items: list[EvidenceItem] = []
    for raw in (res.output or {}).get("evidence", []) if isinstance(res.output, dict) else []:
        with contextlib.suppress(Exception):
            items.append(EvidenceItem.model_validate(raw))
    return AssignmentResult(assignment_id=f"local_{task.task_id}_{node_id}", worker_id=CONTROL_WORKER_ID, result=res, evidence=items)


class ControlActivities:
    def __init__(self, deps: ControlDeps) -> None:
        self.d = deps

    def all(self) -> list[Callable[..., Any]]:
        return [
            self.classify_and_assess, self.retrieve_context, self.plan, self.critique, self.prepare_approvals,
            self.reconcile_approvals, self.run_node, self.build_rollback, self.cancel_assignments, self.judge_and_record,
            self.set_status, self.register_task,
        ]

    @contextlib.contextmanager
    def _span(self, name: str, task: Task) -> Iterator[None]:
        with self._span_ids(name, _trace(task), task.tenant_id, task.task_id, task.workflow_id):
            yield

    @contextlib.contextmanager
    def _span_ids(self, name: str, trace: TraceContext, tenant_id: str, task_id: str, workflow_id: str | None) -> Iterator[None]:
        with bind_trace(trace, tenant_id=tenant_id, task_id=task_id, workflow_id=workflow_id, span_id=root_span_id(trace.trace_id)):
            if self.d.spans is None:
                yield
            else:
                with self.d.spans.span(name):
                    yield

    # ------------------------------------------------------------------ classify -> risk
    @activity.defn(name=T.ACT_CLASSIFY)
    async def classify_and_assess(self, inp: T.TaskWorkflowInput) -> T.ClassifyResult:
        task = inp.task
        with self._span("control.classify_and_assess", task):
            event = inp.event or Event(tenant_id=task.tenant_id, channel=Channel.INTERNAL, kind=EventKind.SYSTEM, text=task.goal, trace=task.trace)
            intent = await self.d.intent.classify(event) if (inp.event is not None or task.family.value == "general") else None
            if intent is None:
                from zeus.contracts.models import Intent

                intent = Intent(event_id=event.event_id, tenant_id=task.tenant_id, family=task.family, summary=task.goal[:200], confidence=1.0, classifier="task")
            assessment = await self.d.risk.assess(event, intent)
            task = task.model_copy(
                update={
                    "family": intent.family,
                    "risk": RiskLevel.max(task.risk, assessment.level),
                    "intent_id": task.intent_id or intent.intent_id,
                    "workflow_id": task.workflow_id or task.task_id,
                    "trace": _trace(task),
                    "status": TaskStatus.PENDING,
                }
            )
            await self.d.store.put_task(task)
            return T.ClassifyResult(task=task, denied=is_denied(assessment), reasons=assessment.reasons)

    # ------------------------------------------------------------------ context / plan / critique
    @activity.defn(name=T.ACT_CONTEXT)
    async def retrieve_context(self, task: Task) -> ContextPacket:
        with self._span("control.retrieve_context", task):
            if self.d.retriever is None:
                return ContextPacket(tenant_id=task.tenant_id, task_id=task.task_id, goal=task.goal)
            return await self.d.retriever.build_context(RetrievalQuery(tenant_id=task.tenant_id, text=task.goal), task)

    @activity.defn(name=T.ACT_PLAN)
    async def plan(self, task: Task, context: ContextPacket) -> Plan:
        with self._span("control.plan", task):
            plan = await self.d.planner.plan(task, context)
            await self.d.store.set_task_status(task.task_id, TaskStatus.PLANNED)
            for n in plan.graph.nodes:
                await self.d.store.save_node(task.task_id, n)
            return plan

    @activity.defn(name=T.ACT_CRITIQUE)
    async def critique(self, task: Task, plan: Plan, context: ContextPacket) -> Critique:
        with self._span("control.critique", task):
            return await self.d.critic.critique(plan, context)

    # ------------------------------------------------------------------ approvals
    @activity.defn(name=T.ACT_APPROVALS)
    async def prepare_approvals(self, task: Task, plan: Plan, timeout_s: int | None) -> T.ApprovalBundle:
        d = self.d
        cfg = d.gateway.policy.config
        timeout = timeout_s if timeout_s is not None else cfg.approval_timeout_s
        out = T.ApprovalBundle()
        existing = {a.action.action_id: a for a in await d.approvals.list(task.tenant_id)}
        with self._span("control.prepare_approvals", task):
            for node in plan.graph.nodes:
                action, risk, summary = node.action, node.risk, None
                spec = d.gateway.spec(action.name) if action else None
                if action is not None:
                    if spec is None:
                        out.denied_reasons.append(f"node {node.node_id}: action {action.name} không có trong registry")
                        continue
                    decision = d.gateway.policy.evaluate(action, spec, task)
                    if decision.effect is PolicyEffect.DENY:
                        out.denied_reasons.append(f"node {node.node_id}: {'; '.join(decision.reasons)} [{','.join(decision.rule_ids)}]")
                        continue
                    if decision.effect is not PolicyEffect.REQUIRE_APPROVAL:
                        continue
                    risk = effective_risk(spec, cfg)
                    summary = f"Duyệt bước '{node.title}' ({spec.name}, {risk.value}) cho task: {task.goal[:120]}"
                elif node.risk >= cfg.approval_min:
                    action = TypedAction(tenant_id=task.tenant_id, task_id=task.task_id, name="task.step", args={"node_id": node.node_id, "title": node.title}, requested_by="planner")
                    summary = f"Duyệt bước thủ công '{node.title}' ({node.risk.value}) cho task: {task.goal[:120]}"
                else:
                    continue
                prev = existing.get(action.action_id)
                if prev is not None:
                    out.requests.append(prev)
                    continue
                req = await d.approvals.request(
                    ApprovalRequest(tenant_id=task.tenant_id, task_id=task.task_id, action=action, risk=risk, summary_vi=summary or "Cần duyệt", expires_at=utcnow() + timedelta(seconds=timeout))
                )
                out.requests.append(req)
        return out

    @activity.defn(name=T.ACT_RECONCILE)
    async def reconcile_approvals(self, tenant_id: str, approval_ids: list[str], expire: bool) -> T.ApprovalStates:
        states: dict[str, str] = {}
        for aid in approval_ids:
            req = await self.d.approvals.get(aid)
            if req is not None and req.status is ApprovalStatus.PENDING and expire:
                req = await self.d.approvals.expire(aid)
            states[aid] = req.status.value if req else "MISSING"
        return T.ApprovalStates(states=states)

    # ------------------------------------------------------------------ node execution
    @activity.defn(name=T.ACT_RUN_NODE)
    async def run_node(self, task: Task, node: TaskNode) -> AssignmentResult:
        with self._span(f"control.run_node.{node.node_id}", task):
            res = await self._run_node(task, node)
        if res is None:  # đã xếp vào hàng đợi worker: hoàn thành bất đồng bộ (ngoài span để span không bị đánh dấu lỗi)
            activity.raise_complete_async()
        return res

    async def _run_node(self, task: Task, node: TaskNode) -> AssignmentResult | None:
        d = self.d
        await d.store.set_task_status(task.task_id, TaskStatus.RUNNING)
        action = node.action
        if action is None:
            res = _local_result(node.node_id, task, ActionResult(action_id=f"noop_{task.task_id}_{node.node_id}", ok=True, output={"noop": True}, executed_by=CONTROL_WORKER_ID))
            await self._persist_node(task, node, res)
            return res
        try:
            spec, _ = await d.gateway.authorize(action, task)
        except (PolicyDenied, ApprovalRequired) as exc:
            res = _local_result(node.node_id, task, ActionResult(action_id=action.action_id, ok=False, error=f"{type(exc).__name__}: {exc}", executed_by=CONTROL_WORKER_ID))
            await self._persist_node(task, node, res)
            return res
        if not (spec.required_capabilities or node.required_capabilities):
            result = await d.gateway.execute(action, task)  # chạy phía control (ERP API, kênh...), có audit/idempotency
            res = _local_result(node.node_id, task, result)
            await self._persist_node(task, node, res)
            return res
        return await self._dispatch(task, node, action, spec.timeout_s, list({*spec.required_capabilities, *node.required_capabilities}))

    async def _dispatch(self, task: Task, node: TaskNode, action: TypedAction, timeout_s: int, caps: list[str]) -> None:
        d = self.d
        if d.scheduler is None or d.workers is None or d.queue is None:
            raise ApplicationError("dispatch chưa được cấu hình (scheduler/workers/queue)", non_retryable=True)
        info = activity.info()
        workers = [w for w in await d.workers.list() if w.status is not WorkerStatus.OFFLINE]
        hbs = {}
        for w in workers:
            hb = await d.workers.last_heartbeat(w.worker_id)
            if hb is not None:
                hbs[w.worker_id] = hb
        sched_node = node.model_copy(update={"required_capabilities": caps})
        decision = await d.scheduler.schedule(task, sched_node, workers, hbs)
        if decision.worker_id is None:
            # Không worker đủ điều kiện: retry có backoff của Temporal = xếp hàng, không mất task.
            raise ApplicationError(f"chưa có worker phù hợp cho node {node.node_id}: {decision.reason}")
        asg = TaskAssignment(
            assignment_id=assignment_id(task.task_id, node.node_id, info.attempt),
            task_id=task.task_id,
            node_id=node.node_id,
            action=action,
            timeout_s=timeout_s,
            lease_expires_at=utcnow() + timedelta(seconds=timeout_s),
            trace=_trace(task),
            schedule_decision_id=decision.decision_id,
            attempt=info.attempt,
        )
        ctx = {"family": task.family.value, "urgency": task.urgency, "risk": node.risk.value, "required_capabilities": caps}
        if _accepts_context(d.queue):  # PgAssignmentQueue (C): lưu ngữ cảnh để giao lại đúng capability khi worker mất tín hiệu
            await d.queue.enqueue(asg, decision.worker_id, task_token=info.task_token, context=ctx)  # type: ignore[call-arg]
        else:
            await d.queue.enqueue(asg, decision.worker_id, task_token=info.task_token)
        await d.store.save_node(task.task_id, node.model_copy(update={"status": TaskStatus.SCHEDULED}), attempts=info.attempt)

    async def _persist_node(self, task: Task, node: TaskNode, res: AssignmentResult) -> None:
        status = TaskStatus.SUCCEEDED if res.result.ok else TaskStatus.FAILED
        await self.d.store.save_node(task.task_id, node.model_copy(update={"status": status}), result=res.result.model_dump(mode="json"))

    @activity.defn(name=T.ACT_ROLLBACK)
    async def build_rollback(self, req: T.RollbackRequest) -> TaskNode | None:
        action = req.node.action
        spec = self.d.gateway.spec(action.name) if action else None
        if action is None or spec is None or not spec.rollback_action or not req.run.result.result.ok:
            return None
        rb_spec = self.d.gateway.spec(spec.rollback_action)
        if rb_spec is None:
            return None
        rb = TypedAction(
            tenant_id=action.tenant_id, task_id=action.task_id, name=rb_spec.name, version=rb_spec.version,
            args={"original_action_id": action.action_id, "rollback_ref": req.run.result.result.rollback_ref, **({"args": action.args} if not rb_spec.input_schema else {})},
            idempotency_key=f"{action.action_id}:rollback", requested_by="saga", trace=action.trace,
        )
        return TaskNode(node_id=f"{req.node.node_id}.rollback", title=f"Hoàn tác: {req.node.title}", family=req.node.family, action=rb, risk=req.node.risk, required_capabilities=req.node.required_capabilities)

    @activity.defn(name=T.ACT_CANCEL_ASSIGN)
    async def cancel_assignments(self, task: Task, node_ids: list[str]) -> int:
        if self.d.queue is None:
            return 0
        n = 0
        for nid in node_ids:
            for attempt in range(1, self.d.max_assignment_attempts + 1):
                await self.d.queue.cancel(assignment_id(task.task_id, nid, attempt))
                n += 1
        return n

    # ------------------------------------------------------------------ judge + evidence + outcome
    @activity.defn(name=T.ACT_JUDGE)
    async def judge_and_record(self, req: T.JudgeRequest) -> T.JudgeOutput:
        d, task = self.d, req.task
        with self._span("control.judge_and_record", task):
            actions = [r.result.result for r in req.results]
            items = [e for r in req.results for e in r.result.evidence]
            workers = [r.result.worker_id for r in req.results if r.result.worker_id != CONTROL_WORKER_ID]
            planner = req.planner or ""
            vendor = planner_vendor(planner)
            provider = ProviderKind(planner.split(":", 1)[0]) if vendor else None
            model_name = planner.split(":", 1)[1] if vendor else None
            outcome = derive_outcome(items, [], all(a.ok for a in actions)) if actions and not req.cancelled else Outcome.UNVERIFIED
            rid = "evr_" + hashlib.sha256(f"{task.task_id}".encode()).hexdigest()[:32]
            feats = req.route_features
            rec = EvidenceRecord(
                record_id=rid,
                trace_id=_trace(task).trace_id,
                tenant_id=task.tenant_id,
                workflow_id=task.workflow_id,
                task_id=task.task_id,
                task_family=task.family,
                goal=task.goal,
                model_provider=provider,
                model_name=model_name,
                playbook_version=planner if planner and not vendor else None,
                route_id=req.route_id,
                tools=sorted({a.name for r in req.results if (a := r.node.action)}),
                worker_id=workers[0] if workers else None,
                schedule_decision_id=next((str(r.result.metrics["schedule_decision_id"]) for r in req.results if "schedule_decision_id" in r.result.metrics), None),
                actions=actions,
                evidence=items,
                latency_ms=max(0, req.finished_at_ms - req.started_at_ms),
                cost_usd=float(feats.get("cost_usd", 0.0)),
                human_intervention=req.human_intervention,
                rollback_performed=req.rollback_performed,
                final_outcome=outcome,
                verified_by="deterministic",
            )
            judge: Judge = d.judge
            makers = {p for p in (provider,) if p is not None}
            if isinstance(judge, DeterministicJudge) and makers:
                judge = judge.for_makers(makers)
            if req.abort_reason or req.cancelled:
                reason = req.abort_reason or "task bị huỷ"
                verdict = Verdict(task_id=task.task_id, decision=VerdictDecision.FAIL if req.cancelled else req.abort_decision, outcome=Outcome.UNVERIFIED, judge="control", reasons=[reason, *req.notes])
            else:
                verdict = await judge.judge(task, [rec])
                if req.notes:
                    verdict = verdict.model_copy(update={"reasons": [*verdict.reasons, *req.notes]})
            final = rec.model_copy(update={"final_outcome": verdict.outcome, "verified_by": verdict.judge})
            final = EvidenceRecord.model_validate(final.model_dump(mode="python"))  # chạy lại validator "claims are not evidence"
            if await d.evidence.get(final.record_id, tenant_id=task.tenant_id) is None:
                await d.evidence.put(final)
            await d.outcomes.record(final)
            status = (
                TaskStatus.CANCELLED if req.cancelled
                else req.abort_status if req.abort_reason
                else TaskStatus.ROLLED_BACK if req.rollback_performed
                else TaskStatus.SUCCEEDED if verdict.decision is VerdictDecision.PASS
                else TaskStatus.FAILED
            )
            await d.store.set_task_status(task.task_id, status)
            for r in req.results:  # node dispatch bất đồng bộ chỉ biết kết quả khi workflow đã thu về
                await self._persist_node(task, r.node, r.result)
            self._emit_root_span(task, req, verdict)
            return T.JudgeOutput(verdict=verdict, record=final)

    def _emit_root_span(self, task: Task, req: T.JudgeRequest, verdict: Verdict) -> None:
        if self.d.spans is None:
            return
        tr = _trace(task)
        span = Span(
            trace_id=tr.trace_id, span_id=root_span_id(tr.trace_id), name="task.lifecycle", kind=SpanKind.INTERNAL,
            start_time_unix_nano=req.started_at_ms * 1_000_000, end_time_unix_nano=max(req.finished_at_ms, req.started_at_ms) * 1_000_000,
            status_code=SpanStatus.OK if verdict.decision is VerdictDecision.PASS else SpanStatus.ERROR,
            attributes={ATTR_TENANT: task.tenant_id, ATTR_TASK: task.task_id, ATTR_WORKFLOW: task.workflow_id, "zeus.family": task.family.value,
                        "zeus.risk": task.risk.value, "zeus.verdict": verdict.decision.value, "zeus.outcome": verdict.outcome.value},
            resource=dict(self.d.spans.resource),
        )
        for exp in self.d.spans.exporters:
            with contextlib.suppress(Exception):
                exp.export([span])

    @activity.defn(name=T.ACT_STATUS)
    async def set_status(self, task_id: str, status: TaskStatus) -> None:
        await self.d.store.set_task_status(task_id, status)

    @activity.defn(name=T.ACT_REGISTER)
    async def register_task(self, task: Task) -> Task:
        await self.d.store.put_task(task)
        return task
