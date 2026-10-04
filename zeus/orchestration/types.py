"""Kiểu dữ liệu đi qua Temporal (pydantic data converter) cho TaskWorkflow."""

from __future__ import annotations

from pydantic import Field

from zeus.contracts.api import AssignmentResult
from zeus.contracts.models import ApprovalRequest, Event, EvidenceRecord, Task, TaskNode, TaskStatus, Verdict, VerdictDecision, ZeusModel

WORKFLOW_NAME = "TaskWorkflow"
SCHEDULED_WORKFLOW_NAME = "ScheduledTaskWorkflow"
SIGNAL_APPROVAL = "approval_decision"
SIGNAL_CANCEL = "cancel"
QUERY_STATUS = "status"

MAX_NODE_ATTEMPTS = 6

ACT_CLASSIFY = "zeus.control.classify_and_assess"
ACT_CONTEXT = "zeus.control.retrieve_context"
ACT_PLAN = "zeus.control.plan"
ACT_CRITIQUE = "zeus.control.critique"
ACT_APPROVALS = "zeus.control.prepare_approvals"
ACT_RECONCILE = "zeus.control.reconcile_approvals"
ACT_RUN_NODE = "zeus.control.run_node"
ACT_ROLLBACK = "zeus.control.build_rollback"
ACT_CANCEL_ASSIGN = "zeus.control.cancel_assignments"
ACT_JUDGE = "zeus.control.judge_and_record"
ACT_STATUS = "zeus.control.set_status"
ACT_REGISTER = "zeus.control.register_task"


class TaskWorkflowInput(ZeusModel):
    task: Task
    event: Event | None = None
    approval_timeout_s: int | None = None  # None => policy.approval.timeout_s
    node_timeout_s: int = 900  # trần cho 1 lần chạy node (gồm chờ worker hoàn thành bất đồng bộ)
    retry_initial_s: float = 2.0
    max_activity_attempts: int = 5


class ClassifyResult(ZeusModel):
    task: Task
    denied: bool = False
    reasons: list[str] = Field(default_factory=list)


class ApprovalBundle(ZeusModel):
    requests: list[ApprovalRequest] = Field(default_factory=list)
    denied_reasons: list[str] = Field(default_factory=list)
    timeout_s: int = 3600  # hạn chờ duyệt thực tế (lấy từ policy khi workflow không ép), workflow dùng đúng giá trị này


class ApprovalStates(ZeusModel):
    """approval_id -> trạng thái hiện tại trong store (đối chiếu khi signal có thể bị lỡ)."""

    states: dict[str, str] = Field(default_factory=dict)


class NodeOutcome(ZeusModel):
    node_id: str
    ok: bool
    error: str | None = None
    rolled_back: bool = False


class NodeRun(ZeusModel):
    node: TaskNode
    result: AssignmentResult


class JudgeRequest(ZeusModel):
    task: Task
    planner: str | None = None
    route_features: dict[str, float] = Field(default_factory=dict)
    route_id: str | None = None
    results: list[NodeRun] = Field(default_factory=list)
    rollback_performed: bool = False
    human_intervention: bool = False
    cancelled: bool = False
    started_at_ms: int = 0
    finished_at_ms: int = 0
    notes: list[str] = Field(default_factory=list)
    failed_nodes: list[TaskNode] = Field(default_factory=list)  # node ném exception (không có kết quả): task KHÔNG được PASS
    skipped_node_ids: list[str] = Field(default_factory=list)  # node không chạy do lỗi/huỷ: task KHÔNG được PASS
    abort_reason: str | None = None  # dừng sớm (bị từ chối/chặn/hết hạn duyệt): verdict do control quyết định, không cần judge
    abort_decision: VerdictDecision = VerdictDecision.FAIL
    abort_status: TaskStatus = TaskStatus.FAILED


class JudgeOutput(ZeusModel):
    verdict: Verdict
    record: EvidenceRecord | None = None


class RollbackRequest(ZeusModel):
    task: Task
    node: TaskNode
    run: NodeRun
