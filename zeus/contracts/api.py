"""Control API + Worker API: đường dẫn và schema dùng chung (FROZEN v1.0 — Phase 0).

- Control API (owner A, zeus/api): ingress event, task, approval, evidence, worker list. Workbench (D) gọi.
- Worker API (owner C, zeus/workers phía server + zeus_worker phía VM): register/heartbeat/poll/result.
- Channel webhooks (owner D, zeus/channels): /hooks/<channel>.

Mô hình dispatch (ADR-011): activity Temporal phía control tạo TaskAssignment kèm ``task_token``
rồi hoàn thành bất đồng bộ (async activity completion) khi worker POST kết quả. Worker KHÔNG cần
Temporal SDK, KHÔNG giữ API key provider; chỉ nói HTTP với Worker API bằng token riêng của nó.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from zeus.contracts.models import (
    ActionResult,
    ApprovalRequest,
    ArtifactRef,
    Event,
    EvidenceItem,
    TaskStatus,
    TraceContext,
    TypedAction,
    WorkerHeartbeat,
    WorkerInfo,
    ZeusModel,
    utcnow,
)

API_VERSION = "v1"

# --------------------------------------------------------------------------- headers
HEADER_TENANT = "X-Zeus-Tenant"
HEADER_TRACE = "traceparent"  # W3C Trace Context
HEADER_WORKER_TOKEN = "X-Zeus-Worker-Token"
HEADER_IDEMPOTENCY = "Idempotency-Key"

# --------------------------------------------------------------------------- Temporal
TEMPORAL_NAMESPACE_DEFAULT = "default"
TASK_QUEUE_CONTROL = "zeus-control"  # workflows + activities phía control plane (A)
TASK_QUEUE_BRAIN = "zeus-brain"  # activities B (retrieval, evidence, learning)
TASK_QUEUE_DISPATCH = "zeus-dispatch"  # activity dispatch->worker (C) dùng async completion


class Paths:
    """Đường dẫn HTTP. Đổi = CONTRACT_CHANGE_REQUEST."""

    HEALTHZ = "/healthz"
    READYZ = "/readyz"

    # Control API (A)
    EVENTS = "/api/v1/events"
    TASKS = "/api/v1/tasks"
    TASK = "/api/v1/tasks/{task_id}"
    TASK_EVIDENCE = "/api/v1/tasks/{task_id}/evidence"
    TASK_CANCEL = "/api/v1/tasks/{task_id}/cancel"
    APPROVALS = "/api/v1/approvals"
    APPROVAL_DECISION = "/api/v1/approvals/{approval_id}/decision"
    EVIDENCE = "/api/v1/evidence/{record_id}"
    WORKERS = "/api/v1/workers"
    ROUTER_STATS = "/api/v1/router/stats"

    # Worker API (C)
    WORKER_REGISTER = "/worker/v1/register"
    WORKER_HEARTBEAT = "/worker/v1/heartbeat"
    WORKER_POLL = "/worker/v1/poll"
    WORKER_RESULT = "/worker/v1/assignments/{assignment_id}/result"
    WORKER_CANCEL_ACK = "/worker/v1/assignments/{assignment_id}/cancel-ack"
    WORKER_ARTIFACTS = "/worker/v1/artifacts"

    # Channel webhooks (D)
    HOOK_ZALO_BOT = "/hooks/zalo-bot"
    HOOK_ZALO_OA = "/hooks/zalo-oa"
    HOOK_MESSENGER = "/hooks/messenger"
    HOOK_SHOPEE = "/hooks/shopee"

    # Workbench UI (D)
    WORKBENCH = "/wb"


# --------------------------------------------------------------------------- Control API schemas


class EventIngestRequest(ZeusModel):
    event: Event


class EventIngestResponse(ZeusModel):
    event_id: str
    accepted: bool
    duplicate: bool = False
    task_id: str | None = None
    workflow_id: str | None = None


class TaskSummary(ZeusModel):
    task_id: str
    tenant_id: str
    family: str
    goal: str
    risk: str
    status: TaskStatus
    created_at: datetime
    workflow_id: str | None = None


class TaskListResponse(ZeusModel):
    items: list[TaskSummary]
    next_cursor: str | None = None


class ApprovalListResponse(ZeusModel):
    items: list[ApprovalRequest]


class ErrorBody(ZeusModel):
    error: str
    detail: str | None = None
    trace_id: str | None = None


# --------------------------------------------------------------------------- Worker API schemas


class WorkerRegisterRequest(ZeusModel):
    info: WorkerInfo


class WorkerRegisterResponse(ZeusModel):
    worker_id: str
    heartbeat_interval_s: int = 15
    poll_wait_s: int = 20
    accepted: bool = True


class HeartbeatResponse(ZeusModel):
    ack: bool = True
    drain: bool = False
    cancel_assignment_ids: list[str] = Field(default_factory=list)


class PollRequest(ZeusModel):
    worker_id: str
    max_assignments: int = Field(default=1, ge=1, le=16)
    wait_s: int = Field(default=20, ge=0, le=60)


class TaskAssignment(ZeusModel):
    assignment_id: str
    task_id: str
    node_id: str | None = None
    action: TypedAction
    timeout_s: int = Field(default=600, gt=0)
    lease_expires_at: datetime
    trace: TraceContext
    schedule_decision_id: str | None = None
    attempt: int = 1


class PollResponse(ZeusModel):
    assignments: list[TaskAssignment] = Field(default_factory=list)


class AssignmentResult(ZeusModel):
    assignment_id: str
    worker_id: str
    result: ActionResult
    log_ref: str | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    finished_at: datetime = Field(default_factory=utcnow)
    metrics: dict[str, Any] = Field(default_factory=dict)


class CancelAck(ZeusModel):
    assignment_id: str
    worker_id: str
    cancelled: bool
    detail: str | None = None


class Ack(ZeusModel):
    ok: bool = True
    detail: str | None = None


__all__ = [
    "API_VERSION",
    "HEADER_TENANT",
    "HEADER_TRACE",
    "HEADER_WORKER_TOKEN",
    "HEADER_IDEMPOTENCY",
    "TASK_QUEUE_CONTROL",
    "TASK_QUEUE_BRAIN",
    "TASK_QUEUE_DISPATCH",
    "TEMPORAL_NAMESPACE_DEFAULT",
    "Paths",
    "EventIngestRequest",
    "EventIngestResponse",
    "TaskSummary",
    "TaskListResponse",
    "ApprovalListResponse",
    "ErrorBody",
    "WorkerRegisterRequest",
    "WorkerRegisterResponse",
    "HeartbeatResponse",
    "PollRequest",
    "TaskAssignment",
    "PollResponse",
    "AssignmentResult",
    "CancelAck",
    "Ack",
    "WorkerHeartbeat",
]
