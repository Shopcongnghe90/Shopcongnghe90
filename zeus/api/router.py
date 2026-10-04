"""Control API router ``/api/v1/*`` (Paths trong zeus.contracts.api). Integrator: ``app.include_router(router)`` và
``install(app, services)`` để gắn ControlServices vào ``app.state.control``. Mọi truy vấn lọc theo tenant (header X-Zeus-Tenant)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from pydantic import Field

from zeus.contracts.api import (
    HEADER_IDEMPOTENCY,
    HEADER_TENANT,
    ApprovalListResponse,
    EventIngestRequest,
    EventIngestResponse,
    Paths,
    TaskListResponse,
    TaskSummary,
)
from zeus.contracts.interfaces import ApprovalStore, EvidenceStore, OutcomeRecorder, WorkerRegistry
from zeus.contracts.models import (
    DEFAULT_TENANT,
    ApprovalDecision,
    ApprovalStatus,
    Event,
    EvidenceRecord,
    Task,
    TaskStatus,
    ZeusModel,
)
from zeus.gateway.gateway import EventGateway
from zeus.gateway.store import ControlStore, UnknownTenant

log = logging.getLogger(__name__)

_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")


class WorkflowControl(Protocol):
    async def start(self, task: Task, event: Event | None) -> str: ...

    async def signal_approval(self, task_id: str, decision: ApprovalDecision) -> None: ...

    async def signal_cancel(self, task_id: str, reason: str) -> None: ...


class NullWorkflowControl:
    """Không có Temporal (test API thuần): ghi lại lời gọi."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.approvals: list[tuple[str, ApprovalDecision]] = []
        self.cancels: list[tuple[str, str]] = []

    async def start(self, task: Task, event: Event | None) -> str:
        self.started.append(task.task_id)
        return task.task_id

    async def signal_approval(self, task_id: str, decision: ApprovalDecision) -> None:
        self.approvals.append((task_id, decision))

    async def signal_cancel(self, task_id: str, reason: str) -> None:
        self.cancels.append((task_id, reason))


class TemporalWorkflowControl:
    def __init__(self, client: Any, task_queue: str | None = None) -> None:
        self.client = client
        self.task_queue = task_queue

    async def start(self, task: Task, event: Event | None) -> str:
        from zeus.orchestration.client import start_task_workflow

        kw: dict[str, Any] = {"task_queue": self.task_queue} if self.task_queue else {}
        return (await start_task_workflow(self.client, task, event, **kw)).id

    async def signal_approval(self, task_id: str, decision: ApprovalDecision) -> None:
        from zeus.orchestration.client import signal_approval

        try:
            await signal_approval(self.client, task_id, decision)
        except Exception as exc:  # noqa: BLE001
            from temporalio.service import RPCError

            # workflow đã kết thúc giữa chừng: quyết định đã lưu, không còn ai chờ signal => không trả 500 (R11)
            if isinstance(exc, RPCError) and "already completed" in str(exc).lower():
                return
            raise

    async def signal_cancel(self, task_id: str, reason: str) -> None:
        from zeus.orchestration.client import signal_cancel

        await signal_cancel(self.client, task_id, reason)


@dataclass
class ControlServices:
    gateway: EventGateway
    store: ControlStore
    approvals: ApprovalStore
    evidence: EvidenceStore
    outcomes: OutcomeRecorder
    workers: WorkerRegistry
    workflows: WorkflowControl
    audit: Any = None  # AuditSink (tuỳ chọn): ghi mọi quyết định duyệt vào audit_log (UX-02)


class DecisionBody(ZeusModel):
    status: Literal[ApprovalStatus.APPROVED, ApprovalStatus.REJECTED]
    decided_by: str = Field(min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=2000)


_TERMINAL = (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.ROLLED_BACK)


async def audit_decision(audit: Any, req: Any, decision: ApprovalDecision, updated: Any) -> None:
    """Ghi audit_log cho quyết định duyệt (ai duyệt gì, kết quả thật gồm cả EXPIRED). Lỗi ghi audit không được làm mất quyết định đã lưu."""
    if audit is None:
        return
    try:
        await audit.record(
            tenant_id=req.tenant_id, actor=decision.decided_by, action="approval.decide", subject_id=req.approval_id, subject_type="approval",
            risk=req.risk.value, task_id=req.task_id,
            details={"status": updated.status.value, "requested": decision.status.value, "comment": decision.comment, "action": req.action.name},
        )
    except Exception:  # noqa: BLE001
        log.exception("không ghi được audit cho quyết định duyệt %s", req.approval_id)


async def task_finished_expire(store: ControlStore, approvals: Any, req: Any) -> bool:
    """True nếu task của approval đã kết thúc (workflow không còn nhận signal). Approval còn PENDING được đánh dấu EXPIRED
    ngay, để không ghi APPROVED rồi trả 500 vì signal vào workflow đã đóng."""
    if not req.task_id:
        return False
    task = await store.get_task(req.tenant_id, req.task_id)
    if task is None or task.status not in _TERMINAL:
        return False
    if req.status is ApprovalStatus.PENDING and hasattr(approvals, "expire"):
        await approvals.expire(req.approval_id)
    return True


class CancelBody(ZeusModel):
    reason: str = Field(default="", max_length=500)


router = APIRouter()


def install(app: FastAPI, services: ControlServices) -> None:
    app.state.control = services


def _svc(request: Request) -> ControlServices:
    s = getattr(request.app.state, "control", None)
    if s is None:
        raise HTTPException(503, "control services chưa được cấu hình")
    return s


def _tenant(value: str | None) -> str:
    t = value or DEFAULT_TENANT
    if not _TENANT_RE.match(t):
        raise HTTPException(400, "X-Zeus-Tenant không hợp lệ")
    return t


def _summary(t: Task) -> TaskSummary:
    return TaskSummary(task_id=t.task_id, tenant_id=t.tenant_id, family=t.family.value, goal=t.goal, risk=t.risk.value, status=t.status, created_at=t.created_at, workflow_id=t.workflow_id)


@router.post(Paths.EVENTS, response_model=EventIngestResponse)
async def ingest_event(
    body: EventIngestRequest,
    request: Request,
    x_zeus_tenant: str | None = Header(default=None, alias=HEADER_TENANT),
    idempotency_key: str | None = Header(default=None, alias=HEADER_IDEMPOTENCY),
) -> EventIngestResponse:
    s = _svc(request)
    tenant = _tenant(x_zeus_tenant)
    if body.event.tenant_id != tenant:
        raise HTTPException(403, "tenant của event khác header X-Zeus-Tenant")
    try:
        res = await s.gateway.ingest(body.event, idempotency_key)
    except UnknownTenant as exc:
        raise HTTPException(400, f"tenant không tồn tại: {exc}") from exc
    if res.duplicate and res.resume_task is not None:  # lần ingest trước tạo task nhưng workflow chưa start được (R3)
        await s.workflows.start(res.resume_task, None)
    if res.duplicate or res.task is None:
        return EventIngestResponse(event_id=res.event.event_id, accepted=True, duplicate=True, task_id=res.task_id, workflow_id=res.task_id)
    wf_id = await s.workflows.start(res.task, None)
    return EventIngestResponse(event_id=res.event.event_id, accepted=True, duplicate=False, task_id=res.task.task_id, workflow_id=wf_id)


@router.get(Paths.TASKS, response_model=TaskListResponse)
async def list_tasks(request: Request, status: TaskStatus | None = None, limit: int = 50, x_zeus_tenant: str | None = Header(default=None, alias=HEADER_TENANT)) -> TaskListResponse:
    s = _svc(request)
    rows = await s.store.list_tasks(_tenant(x_zeus_tenant), status, max(1, min(limit, 200)))
    return TaskListResponse(items=[_summary(t) for t in rows])


@router.get(Paths.TASK)
async def get_task(task_id: str, request: Request, x_zeus_tenant: str | None = Header(default=None, alias=HEADER_TENANT)) -> dict[str, Any]:
    s = _svc(request)
    t = await s.store.get_task(_tenant(x_zeus_tenant), task_id)
    if t is None:
        raise HTTPException(404, "không thấy task")
    return {"task": t.model_dump(mode="json"), "nodes": [{k: v for k, v in n.items() if k != "data"} for n in await s.store.list_nodes(task_id)]}


@router.get(Paths.TASK_EVIDENCE)
async def task_evidence(task_id: str, request: Request, x_zeus_tenant: str | None = Header(default=None, alias=HEADER_TENANT)) -> list[EvidenceRecord]:
    s = _svc(request)
    tenant = _tenant(x_zeus_tenant)
    if await s.store.get_task(tenant, task_id) is None:
        raise HTTPException(404, "không thấy task")
    return await s.evidence.list_for_task(tenant, task_id)


@router.post(Paths.TASK_CANCEL)
async def cancel_task(task_id: str, request: Request, body: CancelBody | None = None, x_zeus_tenant: str | None = Header(default=None, alias=HEADER_TENANT)) -> dict[str, Any]:
    s = _svc(request)
    t = await s.store.get_task(_tenant(x_zeus_tenant), task_id)
    if t is None:
        raise HTTPException(404, "không thấy task")
    if t.status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.ROLLED_BACK):
        raise HTTPException(409, f"task đã kết thúc ({t.status.value})")
    await s.workflows.signal_cancel(task_id, (body.reason if body else "") or "cancelled via API")
    return {"ok": True, "task_id": task_id}


@router.get(Paths.APPROVALS, response_model=ApprovalListResponse)
async def list_approvals(request: Request, status: ApprovalStatus | None = None, x_zeus_tenant: str | None = Header(default=None, alias=HEADER_TENANT)) -> ApprovalListResponse:
    s = _svc(request)
    return ApprovalListResponse(items=await s.approvals.list(_tenant(x_zeus_tenant), status))


@router.post(Paths.APPROVAL_DECISION)
async def decide_approval(approval_id: str, body: DecisionBody, request: Request, x_zeus_tenant: str | None = Header(default=None, alias=HEADER_TENANT)) -> dict[str, Any]:
    s = _svc(request)
    tenant = _tenant(x_zeus_tenant)
    req = await s.approvals.get(approval_id)
    if req is None or req.tenant_id != tenant:
        raise HTTPException(404, "không thấy yêu cầu duyệt")
    if await task_finished_expire(s.store, s.approvals, req):  # task đã kết thúc: duyệt không còn ý nghĩa (R11)
        raise HTTPException(409, "task đã kết thúc; yêu cầu duyệt đã hết hạn")
    try:
        updated = await s.approvals.decide(ApprovalDecision(approval_id=approval_id, status=body.status, decided_by=body.decided_by, comment=body.comment))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await audit_decision(s.audit, req, ApprovalDecision(approval_id=approval_id, status=body.status, decided_by=body.decided_by, comment=body.comment), updated)
    if updated.task_id and updated.status in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED):
        await s.workflows.signal_approval(updated.task_id, ApprovalDecision(approval_id=approval_id, status=updated.status, decided_by=body.decided_by, comment=body.comment))  # type: ignore[arg-type]
    return {"approval_id": approval_id, "status": updated.status.value}


@router.get(Paths.EVIDENCE)
async def get_evidence(record_id: str, request: Request, x_zeus_tenant: str | None = Header(default=None, alias=HEADER_TENANT)) -> EvidenceRecord:
    s = _svc(request)
    rec = await s.evidence.get(record_id, tenant_id=_tenant(x_zeus_tenant))
    if rec is None or rec.tenant_id != _tenant(x_zeus_tenant):
        raise HTTPException(404, "không thấy bằng chứng")
    return rec


@router.get(Paths.WORKERS)
async def list_workers(request: Request) -> list[dict[str, Any]]:
    s = _svc(request)
    return [w.model_dump(mode="json") for w in await s.workers.list()]


@router.get(Paths.ROUTER_STATS)
async def router_stats(request: Request, x_zeus_tenant: str | None = Header(default=None, alias=HEADER_TENANT)) -> dict[str, Any]:
    s = _svc(request)
    tenant = _tenant(x_zeus_tenant)
    stats = await s.outcomes.stats(tenant_id=tenant)
    routes = await s.store.list_routes(tenant, 50)
    return {
        "stats": [{**st.model_dump(mode="json"), "success_rate": st.success_rate, "cost_per_verified_success": st.cost_per_verified_success} for st in stats],
        "recent_routes": [r["route"].model_dump(mode="json", exclude={"candidates", "fallbacks"}) for r in routes],
    }
