"""Nối hệ thống (integrator): dựng mọi store PostgreSQL + dịch vụ A/B/C/D từ ``Settings``.

Một ``System`` dùng cho cả hai tiến trình:
- API (``zeus.app.main``): Control API ``/api/v1`` (A), Worker API ``/worker/v1`` (C), evidence ``/internal`` (B),
  Workbench ``/wb`` + webhook ``/hooks`` (D).
- Temporal worker control (``zeus.app.worker_main``): ``System.deps`` cho TaskWorkflow + vòng bảo trì dispatcher.

Các mối nối (ADR-018):
- Activity ``run_node`` của TaskWorkflow (A) trả ``AssignmentResult``; Worker API (C) hoàn thành nó qua
  ``ControlPlaneCompleter.complete_assignment`` (giữ evidence/log của worker) và gắn ``schedule_decision_id``.
- Scheduler (C) được bọc để lưu mọi ``ScheduleDecision``; OutcomeRecorder (B) được bọc để ghi nhãn outcome về quyết định đó.
- Artifact của worker đi vào ArtifactStore (B) để EvidenceRecord tham chiếu hợp lệ.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from zeus.api.router import ControlServices, NullWorkflowControl, TemporalWorkflowControl, WorkflowControl
from zeus.brain.embeddings import HashingEmbedding
from zeus.brain.retrieval import PgBrainRetriever
from zeus.brain.store import PgMemoryStore
from zeus.broker.factory import build_broker
from zeus.channels.config import build_adapters, build_outbound, build_context, load_yaml
from zeus.channels.pg_dedupe import PgDedupe
from zeus.channels.router import ChannelsContext
from zeus.config import Settings
from zeus.contracts.api import TASK_QUEUE_CONTROL, AssignmentResult, EventIngestResponse
from zeus.contracts.interfaces import ModelBroker
from zeus.contracts.models import (
    ActionResult,
    ActionSpec,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    DatasetRecord,
    Event,
    EvidenceRecord,
    RiskLevel,
    RouterStat,
    Task,
    TaskFamily,
    TaskGraph,
    TaskNode,
    TaskStatus,
    WorkerHeartbeat,
    WorkerInfo,
    ScheduleDecision,
)
from zeus.evidence.store import ArtifactStore, PgEvidenceStore
from zeus.gateway.gateway import EventGateway
from zeus.gateway.store import PgControlStore
from zeus.intent.engine import DefaultIntentEngine
from zeus.learning.outcomes import PgOutcomeRecorder
from zeus.obs import SpanRecorder
from zeus.orchestration.activities import CONTROL_WORKER_ID, ControlDeps
from zeus.planning.critic import DefaultCritic
from zeus.planning.judge import DeterministicJudge
from zeus.planning.planner import DefaultPlanner
from zeus.policy.approvals import PgApprovalStore
from zeus.policy.budget import PgBudgetLedger
from zeus.policy.engine import DefaultPolicyEngine, PolicyConfig
from zeus.policy.gateway import DefaultToolGateway, PgAudit, RemoteToolProvider
from zeus.risk.engine import DefaultRiskEngine
from zeus.storage.db import aconnect
from zeus.storage.spans import PgSpanExporter
from zeus.workbench.router import WorkbenchContext
from zeus.workbench.security import AuthConfig
from zeus.workers.api import WorkerServices
from zeus.workers.config import load_config as load_workers_config
from zeus.workers.dispatch import Dispatcher
from zeus.workers.queue import PgAssignmentQueue
from zeus.workers.registry import PgWorkerRegistry
from zeus.workers.scheduler import DeterministicScheduler, PgScheduleStore

log = logging.getLogger("zeus.app.system")


# --------------------------------------------------------------------------- danh mục action của thin worker


def worker_action_specs() -> list[ActionSpec]:
    """Typed action chạy trên thin worker (zeus_worker executors), đăng ký phía control qua RemoteToolProvider để
    policy/approval/audit vẫn ở control (ADR-011). Capability ``action:<tên>`` do worker tự quảng bá khi có executor.

    Chỉ đăng ký action mà planner điền được tham số (planner chỉ có goal/task_id): ``test.run`` (bộ test cấu hình sẵn
    trên worker). ``http.check``/``file.checksum``/``repo.tests.run`` cần tham số cụ thể (url/path/repo) mà playbook
    chưa sinh được => để là bước thủ công cho tới khi planner hỗ trợ tham số (EXACT NEXT ACTION trong MASTER_STATE).
    """
    return [
        ActionSpec(
            name="test.run", description="Chạy bộ test đã cấu hình trên worker; bằng chứng TEST_RESULT", risk=RiskLevel.R0,
            input_schema={"type": "object", "properties": {"goal": {"type": "string"}, "task_id": {"type": "string"}}},
            idempotent=True, timeout_s=900, required_capabilities=["action:test.run"], owner="C",
        ),
        ActionSpec(
            name="noop.echo", description="Kiểm tra đường dispatch (không side effect)", risk=RiskLevel.R0,
            input_schema={"type": "object", "properties": {"goal": {"type": "string"}, "task_id": {"type": "string"}}},
            idempotent=True, timeout_s=60, required_capabilities=["action:noop.echo"], owner="C",
        ),
    ]


# --------------------------------------------------------------------------- mối nối A <-> C <-> B


class ControlPlaneCompleter:
    """Hoàn thành activity ``run_node`` (TaskWorkflow, A) đang chờ kết quả worker (async completion, ADR-011/018)."""

    def __init__(self, client: Any, dsn: str) -> None:
        self.client = client
        self.dsn = dsn

    async def _decision_id(self, assignment_id: str) -> str | None:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute("SELECT schedule_decision_id FROM assignments WHERE assignment_id=%s", (assignment_id,))
            row = await cur.fetchone()
            return row["schedule_decision_id"] if row else None

    async def complete_assignment(self, task_token: bytes, res: AssignmentResult) -> None:
        if "schedule_decision_id" not in res.metrics:
            did = await self._decision_id(res.assignment_id)
            if did:
                res = res.model_copy(update={"metrics": {**res.metrics, "schedule_decision_id": did}})
        await self.client.get_async_activity_handle(task_token=task_token).complete(res)

    async def complete(self, task_token: bytes, result: ActionResult) -> None:
        """Đường huỷ/hết lease của C chỉ có ActionResult (action_id = assignment_id): bọc thành AssignmentResult."""
        await self.complete_assignment(
            task_token, AssignmentResult(assignment_id=result.action_id, worker_id=result.executed_by or CONTROL_WORKER_ID, result=result)
        )


class PersistingScheduler:
    """Scheduler cho TaskWorkflow: quyết định deterministic (C) + lưu ScheduleDecision (features/weights/candidates)."""

    def __init__(self, inner: DeterministicScheduler, store: PgScheduleStore) -> None:
        self.inner, self.store = inner, store

    async def schedule(self, task: Task, node: TaskNode, workers: Sequence[WorkerInfo], heartbeats: Mapping[str, WorkerHeartbeat]) -> ScheduleDecision:
        decision = await self.inner.schedule(task, node, workers, heartbeats)
        await self.store.save(decision)
        return decision


class LearningOutcomeRecorder:
    """OutcomeRecorder (B) + nhãn học cho scheduler (C): outcome đã kiểm chứng ghi ngược vào ScheduleDecision."""

    def __init__(self, inner: PgOutcomeRecorder, schedules: PgScheduleStore) -> None:
        self.inner, self.schedules = inner, schedules

    async def record(self, evidence: EvidenceRecord) -> DatasetRecord:
        rec = await self.inner.record(evidence)
        if evidence.schedule_decision_id:
            await self.schedules.record_outcome(evidence.schedule_decision_id, evidence.final_outcome)
        return rec

    async def stats(self, task_family: TaskFamily | None = None, tenant_id: str | None = None) -> list[RouterStat]:
        return await self.inner.stats(task_family, tenant_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


# --------------------------------------------------------------------------- Workbench read model


class PgWorkbenchDataSource:
    """WorkbenchDataSource thật: đọc thẳng các store PostgreSQL của A (task/approval), B (evidence), C (worker)."""

    def __init__(self, store: PgControlStore, approvals: PgApprovalStore, evidence: PgEvidenceStore, workers: PgWorkerRegistry, dsn: str) -> None:
        self.store, self.approvals, self.evidence, self.workers, self.dsn = store, approvals, evidence, workers, dsn

    async def list_tasks(self, tenant_id: str, status: TaskStatus | None = None, limit: int = 50) -> list[Task]:
        return await self.store.list_tasks(tenant_id, status, limit)

    async def get_task(self, tenant_id: str, task_id: str) -> Task | None:
        return await self.store.get_task(tenant_id, task_id)

    async def list_approvals(self, tenant_id: str, status: ApprovalStatus | None = None) -> list[ApprovalRequest]:
        return await self.approvals.list(tenant_id, status)

    async def list_workers(self) -> list[WorkerInfo]:
        return await self.workers.list()

    async def list_evidence(self, tenant_id: str, task_id: str) -> list[EvidenceRecord]:
        return await self.evidence.list_for_task(tenant_id, task_id)

    # -- provider bổ sung cho Workbench (CCR của D: không thêm vào Protocol, tiêm qua WorkbenchContext)
    async def get_graph(self, tenant_id: str, task_id: str) -> TaskGraph | None:
        if await self.store.get_task(tenant_id, task_id) is None:
            return None
        rows = await self.store.list_nodes(task_id)
        nodes = []
        for r in rows:
            data = r.get("data") or {}
            try:
                nodes.append(TaskNode.model_validate({**data, "status": r.get("status") or data.get("status", "PENDING")}))
            except ValueError:
                continue
        if not nodes:
            return None
        ids = {n.node_id for n in nodes}
        nodes = [n.model_copy(update={"depends_on": [d for d in n.depends_on if d in ids]}) for n in nodes]
        try:
            return TaskGraph(task_id=task_id, nodes=nodes)
        except ValueError:
            return None

    async def list_audit(self, tenant_id: str, limit: int = 200) -> list[dict[str, Any]]:
        async with await aconnect(self.dsn, autocommit=True) as conn:
            cur = await conn.execute(
                "SELECT occurred_at, actor, action, subject_type, subject_id, task_id, risk, details FROM audit_log"
                " WHERE tenant_id=%s ORDER BY audit_id DESC LIMIT %s",
                (tenant_id, limit),
            )
            rows = await cur.fetchall()
        out = []
        for r in rows:
            target = r["subject_id"] or r["task_id"] or ""
            if r["subject_type"]:
                target = f"{r['subject_type']}:{target}"
            detail = ", ".join(f"{k}={v}" for k, v in list((r["details"] or {}).items())[:6])
            out.append({"at": r["occurred_at"].strftime("%Y-%m-%d %H:%M:%S"), "actor": r["actor"], "action": r["action"],
                        "target": target, "detail": (f"[{r['risk']}] " if r["risk"] else "") + detail})
        return out


# --------------------------------------------------------------------------- mặt tiền ingest/duyệt dùng chung


class ControlFacade:
    """Ingest + duyệt dùng chung cho Workbench (D) và webhook (D): cùng luồng với Control API (A)."""

    def __init__(self, services: ControlServices) -> None:
        self.s = services

    async def ingest(self, event: Event) -> EventIngestResponse:
        res = await self.s.gateway.ingest(event)
        if res.duplicate or res.task is None:
            return EventIngestResponse(event_id=res.event.event_id, accepted=True, duplicate=True, task_id=res.task_id, workflow_id=res.task_id)
        wf_id = await self.s.workflows.start(res.task, None)
        return EventIngestResponse(event_id=res.event.event_id, accepted=True, duplicate=False, task_id=res.task.task_id, workflow_id=wf_id)

    async def decide(self, decision: ApprovalDecision, tenant_id: str | None = None) -> ApprovalRequest:
        req = await self.s.approvals.get(decision.approval_id)
        if req is None or (tenant_id is not None and req.tenant_id != tenant_id):
            raise KeyError(decision.approval_id)
        updated = await self.s.approvals.decide(decision)
        if updated.task_id and updated.status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
            await self.s.workflows.signal_approval(updated.task_id, decision.model_copy(update={"status": updated.status}))
        return updated


# --------------------------------------------------------------------------- dựng hệ thống


@dataclass
class System:
    settings: Settings
    dsn: str
    control: ControlServices
    facade: ControlFacade
    deps: ControlDeps
    worker_services: WorkerServices
    dispatcher: Dispatcher
    workbench: WorkbenchContext
    channels: ChannelsContext
    evidence: PgEvidenceStore
    artifacts: ArtifactStore
    outcomes: LearningOutcomeRecorder
    schedules: PgScheduleStore
    task_queue: str = TASK_QUEUE_CONTROL


def _usable_broker(settings: Settings, env: Mapping[str, str], policy: PolicyConfig, store: PgControlStore, ledger: PgBudgetLedger) -> ModelBroker | None:
    """Có provider khả dụng (local URL hoặc key cloud được phép) => broker; không có => None: intent/planner/critic/judge
    chạy deterministic (Cloud Exit: không cần Claude Cloud hay key nào)."""
    from zeus.contracts.models import ProviderKind

    has_local = bool(settings.local_llm_url)
    has_cloud = settings.cloud_providers_enabled() and any(
        settings.provider_key(k, env) for k in (ProviderKind.ANTHROPIC, ProviderKind.OPENAI, ProviderKind.GEMINI)
    )
    if not (has_local or has_cloud):
        return None

    async def route_sink(route, request) -> None:  # noqa: ANN001
        await store.save_route(route, request.tenant_id, request.trace.task_id if request.trace else None, request.request_id)

    return build_broker(settings, policy=policy, ledger=ledger, route_sink=route_sink, env=env)


def build_system(
    settings: Settings,
    *,
    temporal_client: Any = None,
    task_queue: str = TASK_QUEUE_CONTROL,
    workflows: WorkflowControl | None = None,
    broker: ModelBroker | None | bool = False,
    auth: AuthConfig | None = None,
    env: Mapping[str, str] | None = None,
    tenant_id: str | None = None,
) -> System:
    """Dựng toàn bộ dịch vụ trên PostgreSQL. ``temporal_client`` None => không start workflow (NullWorkflowControl)
    và Worker API không hoàn thành được activity (chỉ dùng cho test API thuần).
    ``broker=False`` (mặc định) => tự chọn theo cấu hình; ``None`` => buộc chạy deterministic."""
    e = os.environ if env is None else env
    if not settings.db_dsn:
        raise RuntimeError("thiếu ZEUS_DB_DSN: hệ thống tích hợp cần PostgreSQL")
    dsn = settings.db_dsn
    policy_cfg = PolicyConfig.load(settings.policy_config)

    # ---- A: control plane stores
    store, approvals, audit, ledger = PgControlStore(dsn), PgApprovalStore(dsn), PgAudit(dsn), PgBudgetLedger(dsn)
    model_broker = _usable_broker(settings, e, policy_cfg, store, ledger) if broker is False else broker  # type: ignore[assignment]

    # ---- B: brain / evidence / learning
    brain_cfg = load_yaml(settings.brain_config) if settings.brain_config.exists() else {}
    dim = int(((brain_cfg.get("embedding") or {}).get("dim")) or 256)
    memory = PgMemoryStore(dsn, HashingEmbedding(dim=dim))
    retriever = PgBrainRetriever(memory, candidate_k=int(((brain_cfg.get("retrieval") or {}).get("candidate_k")) or 40),
                                 default_token_budget=int(((brain_cfg.get("retrieval") or {}).get("token_budget")) or 4000))
    evidence = PgEvidenceStore(dsn)
    artifacts = ArtifactStore(dsn, settings.artifacts_path())

    # ---- C: workers
    wcfg = load_workers_config(settings.workers_config)
    registry = PgWorkerRegistry(dsn)
    queue = PgAssignmentQueue(dsn, lease_ttl_s=wcfg.lease_ttl_s, max_attempts=wcfg.max_attempts)
    schedules = PgScheduleStore(dsn)
    scheduler = DeterministicScheduler(wcfg.scheduler, stats_provider=schedules.success_stats, stale_ttl_s=wcfg.stale_ttl_s)
    outcomes = LearningOutcomeRecorder(PgOutcomeRecorder(dsn, evidence), schedules)
    completer = ControlPlaneCompleter(temporal_client, dsn) if temporal_client is not None else None
    dispatcher = Dispatcher(registry, queue, scheduler, schedules, wcfg, completer)
    worker_services = WorkerServices(registry, queue, wcfg, completer, settings.artifacts_path(), dsn, artifacts)

    # ---- D: kênh (adapter bật khi có secret) + outbound typed action phía control
    ch_cfg = load_yaml(settings.channels_config) if settings.channels_config.exists() else {}
    adapters = build_adapters(ch_cfg, e)
    import httpx

    providers: list[Any] = [RemoteToolProvider(worker_action_specs())]
    if adapters:
        providers.append(build_outbound(ch_cfg, adapters, httpx.AsyncClient(timeout=30), e))

    # ---- A: tool gateway + control deps
    gateway = DefaultToolGateway(providers, DefaultPolicyEngine(policy_cfg), approvals, audit)
    intent, risk = DefaultIntentEngine(model_broker), DefaultRiskEngine()
    deps = ControlDeps(
        intent=intent, risk=risk,
        planner=DefaultPlanner(model_broker, gateway.list_actions),
        critic=DefaultCritic(model_broker, gateway.list_actions),
        judge=DeterministicJudge(model_broker),
        gateway=gateway, approvals=approvals, store=store, evidence=evidence, outcomes=outcomes, retriever=retriever,
        scheduler=PersistingScheduler(scheduler, schedules), workers=registry, queue=queue,
        spans=SpanRecorder([PgSpanExporter(dsn)], {"service.name": "zeus-control"}),
    )
    wf = workflows or (TemporalWorkflowControl(temporal_client, task_queue) if temporal_client is not None else NullWorkflowControl())
    control = ControlServices(gateway=EventGateway(store, intent, risk), store=store, approvals=approvals, evidence=evidence,
                              outcomes=outcomes, workers=registry, workflows=wf)
    facade = ControlFacade(control)

    # ---- D: Workbench + hooks
    data = PgWorkbenchDataSource(store, approvals, evidence, registry, dsn)
    from zeus.contracts.models import DEFAULT_TENANT

    wb_tenant = tenant_id or e.get("ZEUS_WB_TENANT") or DEFAULT_TENANT

    async def wb_decide(decision: ApprovalDecision) -> ApprovalRequest:
        return await facade.decide(decision, wb_tenant)

    workbench = WorkbenchContext(
        data=data, auth=auth or AuthConfig.from_env(e), tenant_id=wb_tenant, ingest=facade.ingest, decide=wb_decide,
        registry=registry, retriever=retriever, recorder=outcomes, graph_provider=data.get_graph, audit_provider=data.list_audit,
    )
    channels = build_context(ch_cfg, facade.ingest, e, dedupe=PgDedupe(dsn, int(ch_cfg.get("dedupe_ttl_s", 86400))))
    return System(settings, dsn, control, facade, deps, worker_services, dispatcher, workbench, channels, evidence, artifacts,
                  outcomes, schedules, task_queue)


__all__ = [
    "ControlFacade", "ControlPlaneCompleter", "LearningOutcomeRecorder", "PersistingScheduler", "PgWorkbenchDataSource",
    "System", "build_system", "worker_action_specs",
]
