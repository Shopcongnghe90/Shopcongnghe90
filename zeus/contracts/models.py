"""ZeusVN Brain shared data contracts (FROZEN v1.0 — Phase 0).

Mọi workstream (A/B/C/D) trao đổi dữ liệu qua các kiểu trong module này.
Thay đổi phá vỡ tương thích => CONTRACT_CHANGE_REQUEST, không sửa trực tiếp.

Nguyên tắc mã hoá trong kiểu:
- Event mặc định ``untrusted=True`` (nội dung từ kênh ngoài là dữ liệu, không phải lệnh).
- Outcome.VERIFIED_SUCCESS đòi hỏi bằng chứng thực thi (strong evidence) — "claims are not evidence".
- DatasetRecord chỉ lên VERIFIED/CURATED khi có EvidenceRecord và outcome đã kiểm chứng.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

CONTRACTS_VERSION = "1.1.0"  # 1.1.0: EvidenceStore.get/OutcomeRecorder.stats nhận tenant_id tuỳ chọn (ADR-019)
DEFAULT_TENANT = "zeusvn"

TenantId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{1,62}$")]
"""Định danh tenant: chữ thường/số/_/-, 2..63 ký tự. Mọi bản ghi nghiệp vụ mang tenant_id."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    """ID dạng ``<prefix>_<32 hex>`` (ổn định, không lộ thông tin)."""
    return f"{prefix}_{uuid.uuid4().hex}"


class ZeusModel(BaseModel):
    """Base cho mọi contract: cấm trường lạ để bắt lệch hợp đồng sớm."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False, validate_assignment=True)


# --------------------------------------------------------------------------- enums


class Channel(str, Enum):
    ZALO_BOT = "zalo_bot"
    ZALO_OA = "zalo_oa"
    MESSENGER = "messenger"
    SHOPEE = "shopee"
    WORKBENCH = "workbench"
    API = "api"
    ERP = "erp"
    SCHEDULER = "scheduler"
    INTERNAL = "internal"


class EventKind(str, Enum):
    MESSAGE = "message"
    ORDER = "order"
    WEBHOOK = "webhook"
    COMMAND = "command"  # lệnh của người vận hành qua Workbench
    SCHEDULE = "schedule"
    SYSTEM = "system"


class EventStage(str, Enum):
    RAW = "RAW"
    NORMALIZED = "NORMALIZED"


class TaskFamily(str, Enum):
    """18 family của eval + general. Dùng làm khoá router stats / scheduler / eval."""

    ERP_BUG = "erp_bug"
    ERP_FEATURE = "erp_feature"
    WEBSITE_EDIT = "website_edit"
    WEBSITE_BUILD = "website_build"
    FRONTEND = "frontend"
    BACKEND = "backend"
    DATABASE = "database"
    CUSTOMER_SUPPORT = "customer_support"
    ZALO_ISSUE = "zalo_issue"
    FACEBOOK_ISSUE = "facebook_issue"
    SHOPEE_ISSUE = "shopee_issue"
    DOMAIN_PROVISIONING = "domain_provisioning"
    DEPLOYMENT = "deployment"
    SECURITY = "security"
    VISUAL_QA = "visual_qa"
    WORKER_SCHEDULING = "worker_scheduling"
    MODEL_ROUTING = "model_routing"
    TOOL_SELECTION = "tool_selection"
    GENERAL = "general"


EVAL_TASK_FAMILIES: tuple[TaskFamily, ...] = tuple(f for f in TaskFamily if f is not TaskFamily.GENERAL)


class RiskLevel(str, Enum):
    """R0 đọc; R1 ghi nội bộ; R2 ghi dữ liệu thật/production; R3 tiền/xoá/secret/ra ngoài."""

    R0 = "R0"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"

    @property
    def rank(self) -> int:
        return int(self.value[1])

    def __ge__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.rank >= other.rank

    def __gt__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.rank > other.rank

    def __le__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.rank <= other.rank

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.rank < other.rank

    @classmethod
    def max(cls, *levels: "RiskLevel") -> "RiskLevel":
        return max(levels, key=lambda r: r.rank) if levels else cls.R0


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    PLANNED = "PLANNED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ROLLED_BACK = "ROLLED_BACK"


class Outcome(str, Enum):
    VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
    VERIFIED_FAILURE = "VERIFIED_FAILURE"
    UNVERIFIED = "UNVERIFIED"


class DatasetStage(str, Enum):
    RAW = "RAW"
    NORMALIZED = "NORMALIZED"
    VERIFIED = "VERIFIED"
    CURATED = "CURATED"


class ProviderKind(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    LOCAL = "local"  # llama.cpp server / OpenAI-compatible trên RTX 3060
    FAKE = "fake"


class ModelRole(str, Enum):
    """Vai trò bootstrap (chỉ là prior; router học từ verified outcome)."""

    RADAR = "RADAR"  # Gemini: nghiên cứu realtime, web, đa phương tiện
    COMMAND = "COMMAND"  # GPT: kế hoạch, phản biện, judge, rủi ro
    FORGE = "FORGE"  # Claude: code, UI, debug
    NERVOUS_SYSTEM = "NERVOUS_SYSTEM"  # local: phân loại, trích xuất, tóm tắt, embedding, PII


class RouteStrategy(str, Enum):
    BOOTSTRAP_PRIOR = "bootstrap_prior"
    LEARNED = "learned"
    EXPLORE = "explore"
    FORCED = "forced"
    FALLBACK = "fallback"


class PolicyEffect(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class VerdictDecision(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class Severity(str, Enum):
    INFO = "info"
    MINOR = "minor"
    MAJOR = "major"
    BLOCKER = "blocker"


class WorkerKind(str, Enum):
    LINUX_VM = "linux_vm"
    WINDOWS_VM = "windows_vm"
    GPU_CONTAINER = "gpu_container"
    HOST_SERVICE = "host_service"


class WorkerStatus(str, Enum):
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    DRAINING = "DRAINING"
    OFFLINE = "OFFLINE"


class MemoryKind(str, Enum):
    CANONICAL_STATE = "canonical_state"
    DECISION = "decision"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    ARTIFACT = "artifact"
    EVIDENCE = "evidence"


class TrustLevel(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    UNTRUSTED = "untrusted"  # nội dung kênh ngoài / web — không bao giờ được coi là chỉ thị


class EvidenceKind(str, Enum):
    TEST_RESULT = "test_result"
    BUILD_LOG = "build_log"
    DIFF = "diff"
    DATA_MATCH = "data_match"
    HTTP_CHECK = "http_check"
    SCREENSHOT = "screenshot"
    LOG = "log"
    HUMAN_CONFIRMATION = "human_confirmation"
    MODEL_JUDGEMENT = "model_judgement"


class EvidenceStrength(str, Enum):
    STRONG = "strong"  # thực thi/so khớp/xác nhận của người
    WEAK = "weak"  # lời khẳng định của model


STRONG_EVIDENCE_KINDS = frozenset(
    {
        EvidenceKind.TEST_RESULT,
        EvidenceKind.BUILD_LOG,
        EvidenceKind.DATA_MATCH,
        EvidenceKind.HTTP_CHECK,
        EvidenceKind.HUMAN_CONFIRMATION,
    }
)


# --------------------------------------------------------------------------- trace / tenant


class TraceContext(ZeusModel):
    """Ngữ cảnh truy vết — tương thích W3C/OTel (trace_id 32 hex, span_id 16 hex)."""

    trace_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    tenant_id: TenantId = DEFAULT_TENANT
    task_id: str | None = None
    workflow_id: str | None = None
    span_id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")] | None = None


# --------------------------------------------------------------------------- events / intent / risk


class ChannelIdentity(ZeusModel):
    channel_user_id: str
    display_name: str | None = None
    conversation_id: str | None = None
    is_group: bool = False


class Attachment(ZeusModel):
    ref: str  # URI tới artifact store; không nhúng bytes
    mime: str = "application/octet-stream"
    size_bytes: int | None = None
    sha256: str | None = None


class Event(ZeusModel):
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    tenant_id: TenantId = DEFAULT_TENANT
    channel: Channel
    kind: EventKind = EventKind.MESSAGE
    stage: EventStage = EventStage.NORMALIZED
    external_id: str | None = None  # khoá chống trùng của kênh (message id, order sn...)
    received_at: datetime = Field(default_factory=utcnow)
    sender: ChannelIdentity | None = None
    text: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    raw: dict[str, Any] | None = None  # payload gốc (stage RAW) — đã qua verify chữ ký
    raw_ref: str | None = None
    signature_verified: bool = False
    untrusted: bool = True
    trace: TraceContext | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def dedupe_key(self) -> str:
        return f"{self.tenant_id}:{self.channel.value}:{self.external_id or self.event_id}"


class Intent(ZeusModel):
    intent_id: str = Field(default_factory=lambda: new_id("int"))
    event_id: str
    tenant_id: TenantId = DEFAULT_TENANT
    family: TaskFamily
    summary: str
    entities: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    needs_clarification: bool = False
    language: str = "vi"
    classifier: str | None = None  # provider/model hoặc "rules"


class RiskAssessment(ZeusModel):
    level: RiskLevel
    reasons: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    pii_detected: bool = False
    data_classes: list[str] = Field(default_factory=list)  # vd: "pii.phone", "finance", "secret"
    injection_suspected: bool = False


# --------------------------------------------------------------------------- tools / actions


class ActionSpec(ZeusModel):
    """Đặc tả typed tool. Mọi side-effect đi qua Tool Gateway theo spec này."""

    name: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")]
    version: str = "1"
    description: str = ""
    risk: RiskLevel
    input_schema: dict[str, Any] = Field(default_factory=dict)  # JSON Schema
    output_schema: dict[str, Any] = Field(default_factory=dict)
    external: bool = False  # tác động ra ngoài hệ thống (gửi tin, gọi API bên thứ 3)
    reversible: bool = True
    rollback_action: str | None = None
    idempotent: bool = False
    timeout_s: int = Field(default=120, gt=0)
    required_capabilities: list[str] = Field(default_factory=list)
    owner: Literal["A", "B", "C", "D", "shared"] = "shared"


class TypedAction(ZeusModel):
    action_id: str = Field(default_factory=lambda: new_id("act"))
    tenant_id: TenantId = DEFAULT_TENANT
    task_id: str | None = None
    name: str
    version: str = "1"
    args: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    requested_by: str = "system"  # agent/model/human id
    dry_run: bool = False
    trace: TraceContext | None = None


class ActionResult(ZeusModel):
    action_id: str
    ok: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime = Field(default_factory=utcnow)
    evidence_ids: list[str] = Field(default_factory=list)
    rollback_ref: str | None = None
    executed_by: str | None = None  # worker_id hoặc "control"


class PolicyDecision(ZeusModel):
    effect: PolicyEffect
    reasons: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    required_approvers: int = 0
    budget_remaining_usd: float | None = None
    redact_pii: bool = False
    allow_cloud: bool = True


class ApprovalRequest(ZeusModel):
    approval_id: str = Field(default_factory=lambda: new_id("apr"))
    tenant_id: TenantId = DEFAULT_TENANT
    task_id: str | None = None
    action: TypedAction
    risk: RiskLevel
    summary_vi: str  # mô tả cho người duyệt, tiếng Việt
    requested_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    comment: str | None = None


class ApprovalDecision(ZeusModel):
    approval_id: str
    status: Literal[ApprovalStatus.APPROVED, ApprovalStatus.REJECTED]
    decided_by: str
    decided_at: datetime = Field(default_factory=utcnow)
    comment: str | None = None


# --------------------------------------------------------------------------- tasks / planning


class Task(ZeusModel):
    task_id: str = Field(default_factory=lambda: new_id("tsk"))
    tenant_id: TenantId = DEFAULT_TENANT
    family: TaskFamily
    goal: str
    risk: RiskLevel = RiskLevel.R0
    status: TaskStatus = TaskStatus.PENDING
    intent_id: str | None = None
    event_id: str | None = None
    parent_task_id: str | None = None
    urgency: int = Field(default=1, ge=0, le=3)  # 0 thấp .. 3 khẩn
    budget_usd: float | None = Field(default=None, ge=0)
    acceptance_criteria: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    deadline: datetime | None = None
    workflow_id: str | None = None
    trace: TraceContext | None = None
    untrusted: bool = False  # nguồn gốc từ event không tin cậy (kênh ngoài): action không-đọc-thuần cần duyệt (P-UNTRUSTED-ORIGIN)


class TaskNode(ZeusModel):
    node_id: str
    title: str
    family: TaskFamily = TaskFamily.GENERAL
    action: TypedAction | None = None
    depends_on: list[str] = Field(default_factory=list)
    risk: RiskLevel = RiskLevel.R0
    required_capabilities: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    max_retries: int = Field(default=1, ge=0, le=5)


class TaskGraphError(ValueError):
    pass


class TaskGraph(ZeusModel):
    """DAG các bước. Hợp lệ khi: node_id duy nhất, depends_on tồn tại, không có chu trình."""

    graph_id: str = Field(default_factory=lambda: new_id("dag"))
    task_id: str
    nodes: list[TaskNode]

    @model_validator(mode="after")
    def _check_dag(self) -> "TaskGraph":
        self.topological_order()
        return self

    def node(self, node_id: str) -> TaskNode:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        raise KeyError(node_id)

    def topological_order(self) -> list[str]:
        ids = [n.node_id for n in self.nodes]
        if len(ids) != len(set(ids)):
            raise TaskGraphError("duplicate node_id in TaskGraph")
        known = set(ids)
        indeg = {i: 0 for i in ids}
        children: dict[str, list[str]] = {i: [] for i in ids}
        for n in self.nodes:
            for d in n.depends_on:
                if d not in known:
                    raise TaskGraphError(f"node {n.node_id!r} depends on unknown node {d!r}")
                if d == n.node_id:
                    raise TaskGraphError(f"node {n.node_id!r} depends on itself")
                indeg[n.node_id] += 1
                children[d].append(n.node_id)
        ready = [i for i in ids if indeg[i] == 0]
        order: list[str] = []
        while ready:
            cur = ready.pop(0)
            order.append(cur)
            for c in children[cur]:
                indeg[c] -= 1
                if indeg[c] == 0:
                    ready.append(c)
        if len(order) != len(ids):
            raise TaskGraphError("TaskGraph contains a cycle")
        return order

    def ready_nodes(self, done: set[str]) -> list[str]:
        """Các node chưa xong mà mọi phụ thuộc đã xong (để chạy song song)."""
        return [n.node_id for n in self.nodes if n.node_id not in done and set(n.depends_on) <= done]

    @property
    def max_risk(self) -> RiskLevel:
        return RiskLevel.max(*(n.risk for n in self.nodes))


class RouteCandidate(ZeusModel):
    provider: ProviderKind
    model: str
    score: float
    prior: float = 0.0
    learned: float | None = None
    est_cost_usd: float | None = None
    reasons: list[str] = Field(default_factory=list)


class RouteDecision(ZeusModel):
    route_id: str = Field(default_factory=lambda: new_id("rte"))
    task_family: TaskFamily
    role: ModelRole | None = None
    provider: ProviderKind
    model: str
    strategy: RouteStrategy = RouteStrategy.BOOTSTRAP_PRIOR
    candidates: list[RouteCandidate] = Field(default_factory=list)
    fallbacks: list[RouteCandidate] = Field(default_factory=list)
    features: dict[str, float] = Field(default_factory=dict)
    policy_version: str = "bootstrap-1"
    decided_at: datetime = Field(default_factory=utcnow)


class Plan(ZeusModel):
    plan_id: str = Field(default_factory=lambda: new_id("pln"))
    task_id: str
    graph: TaskGraph
    rationale: str = ""
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    route: RouteDecision | None = None
    planner: str | None = None  # provider/model/playbook version
    version: int = 1


class CritiqueIssue(ZeusModel):
    severity: Severity
    message: str
    node_id: str | None = None


class Critique(ZeusModel):
    critique_id: str = Field(default_factory=lambda: new_id("crt"))
    plan_id: str
    reviewer: str  # provider/model — phải KHÁC hãng với planner (kiểm chéo)
    reviewer_provider: ProviderKind | None = None
    issues: list[CritiqueIssue] = Field(default_factory=list)
    approve: bool

    @property
    def has_blocker(self) -> bool:
        return any(i.severity is Severity.BLOCKER for i in self.issues)


class Verdict(ZeusModel):
    verdict_id: str = Field(default_factory=lambda: new_id("vrd"))
    task_id: str
    decision: VerdictDecision
    outcome: Outcome
    judge: str
    evidence_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> "Verdict":
        if self.outcome is Outcome.VERIFIED_SUCCESS and not self.evidence_ids:
            raise ValueError("VERIFIED_SUCCESS verdict requires evidence_ids")
        if self.decision is VerdictDecision.PASS and self.outcome is not Outcome.VERIFIED_SUCCESS:
            raise ValueError("PASS decision requires outcome VERIFIED_SUCCESS")
        return self


# --------------------------------------------------------------------------- model broker


class ChatMessage(ZeusModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    untrusted: bool = False  # True => broker bọc trong khối dữ liệu, không coi là chỉ thị


class ModelRequest(ZeusModel):
    request_id: str = Field(default_factory=lambda: new_id("mrq"))
    tenant_id: TenantId = DEFAULT_TENANT
    task_family: TaskFamily = TaskFamily.GENERAL
    role: ModelRole | None = None
    system: str | None = None
    messages: list[ChatMessage]
    max_tokens: int = Field(default=1024, gt=0)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    json_output: bool = False
    required_capabilities: list[str] = Field(default_factory=list)  # vd "vision", "tools", "long_context"
    contains_pii: bool = False
    allow_cloud: bool = True
    preferred: list[ProviderKind] = Field(default_factory=list)
    max_cost_usd: float | None = Field(default=None, ge=0)
    prompt_version: str | None = None
    trace: TraceContext | None = None


class Usage(ZeusModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


class Cost(ZeusModel):
    usd: float = Field(default=0.0, ge=0)
    estimated: bool = True
    pricing_ref: str | None = None  # vd "anthropic:2026-10" — bảng giá trong config/models.yaml

    @staticmethod
    def from_usage(usage: Usage, in_per_mtok: float, out_per_mtok: float, *, batch: bool = False, pricing_ref: str | None = None) -> "Cost":
        usd = (usage.input_tokens * in_per_mtok + usage.output_tokens * out_per_mtok) / 1_000_000
        if batch:
            usd *= 0.5
        return Cost(usd=round(usd, 8), estimated=True, pricing_ref=pricing_ref)


class ModelResponse(ZeusModel):
    request_id: str
    provider: ProviderKind
    model: str
    text: str
    usage: Usage = Field(default_factory=Usage)
    cost: Cost = Field(default_factory=Cost)
    latency_ms: int = 0
    finish_reason: str | None = None
    fallback_chain: list[str] = Field(default_factory=list)  # "provider:model" đã thử trước khi thành công
    route_id: str | None = None


class ProviderCapability(ZeusModel):
    provider: ProviderKind
    model: str
    local: bool = False
    context_window: int | None = None
    max_output_tokens: int | None = None
    supports_tools: bool = False
    supports_vision: bool = False
    supports_json: bool = False
    supports_embeddings: bool = False
    price_in_per_mtok: float | None = None
    price_out_per_mtok: float | None = None
    roles: list[ModelRole] = Field(default_factory=list)
    available: bool = True
    verified_model_id: bool = False  # False => model id "cần điền"/chưa kiểm chứng


# --------------------------------------------------------------------------- workers / scheduling


class WorkerCapability(ZeusModel):
    name: str  # vd "python", "git", "browser", "odoo_client", "gpu_llm", "windows_gui"
    version: str | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)


class GpuInfo(ZeusModel):
    name: str
    vram_mb: int
    driver: str | None = None


class Inventory(ZeusModel):
    hostname: str
    os: str
    arch: str = "x86_64"
    cpu_count: int = Field(ge=1)
    ram_mb: int = Field(ge=1)
    disk_free_gb: float = 0.0
    gpus: list[GpuInfo] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)


class WorkerInfo(ZeusModel):
    worker_id: str
    kind: WorkerKind
    tenant_scope: list[TenantId] = Field(default_factory=lambda: [DEFAULT_TENANT])
    capabilities: list[WorkerCapability] = Field(default_factory=list)
    inventory: Inventory
    status: WorkerStatus = WorkerStatus.ONLINE
    network_zone: Literal["core", "agent", "ops", "erp_test", "dmz"] = "agent"
    data_localities: list[str] = Field(default_factory=list)  # vd "repo:erp-addons", "cache:website-x"
    version: str = "0.1.0"
    registered_at: datetime = Field(default_factory=utcnow)

    def has_capabilities(self, required: list[str]) -> bool:
        names = {c.name for c in self.capabilities}
        return set(required) <= names


class WorkerHeartbeat(ZeusModel):
    worker_id: str
    at: datetime = Field(default_factory=utcnow)
    status: WorkerStatus = WorkerStatus.ONLINE
    cpu_available_pct: float = Field(default=100.0, ge=0, le=100)
    ram_available_mb: int = Field(default=0, ge=0)
    queue_depth: int = Field(default=0, ge=0)
    running_assignment_ids: list[str] = Field(default_factory=list)
    gpu_util_pct: float | None = None
    gpu_mem_free_mb: int | None = None
    recent_error_rate: float = Field(default=0.0, ge=0, le=1)


Unit = Annotated[float, Field(ge=0.0, le=1.0)]


class ScheduleFeatures(ZeusModel):
    """10 yếu tố bootstrap của Resource Scheduler, chuẩn hoá về [0,1] (1 = tốt cho việc chọn).

    ``risk`` và ``failure_penalty``: 1 = rủi ro/phạt cao (trọng số âm trong scorer).
    Vector này được lưu nguyên trong ScheduleDecision để học P(success | task, worker, state).
    """

    capability_match: Unit
    worker_health: Unit
    cpu_available: Unit
    ram_available: Unit
    queue_load: Unit  # 1 = hàng đợi trống
    historical_success: Unit
    data_locality: Unit
    urgency: Unit
    risk: Unit
    failure_penalty: Unit


class ScheduleCandidate(ZeusModel):
    worker_id: str
    score: float
    features: ScheduleFeatures
    rejected_reason: str | None = None


class ScheduleDecision(ZeusModel):
    decision_id: str = Field(default_factory=lambda: new_id("sch"))
    tenant_id: TenantId = DEFAULT_TENANT
    task_id: str
    node_id: str | None = None
    task_family: TaskFamily = TaskFamily.GENERAL
    worker_id: str | None  # None => xếp hàng (không worker nào đủ điều kiện)
    score: float = 0.0
    features: ScheduleFeatures | None = None
    weights: dict[str, float] = Field(default_factory=dict)
    candidates: list[ScheduleCandidate] = Field(default_factory=list)
    policy_version: str = "deterministic-1"
    reason: str = ""
    decided_at: datetime = Field(default_factory=utcnow)
    outcome: Outcome | None = None  # điền sau khi có verified outcome => nhãn học


# --------------------------------------------------------------------------- evidence / learning


class EvidenceItem(ZeusModel):
    evidence_id: str = Field(default_factory=lambda: new_id("evi"))
    kind: EvidenceKind
    ref: str | None = None  # URI artifact store / đường dẫn log
    summary: str = ""
    sha256: str | None = None
    passed: bool | None = None

    @property
    def strength(self) -> EvidenceStrength:
        return EvidenceStrength.STRONG if self.kind in STRONG_EVIDENCE_KINDS else EvidenceStrength.WEAK


class TestRun(ZeusModel):
    __test__ = False  # không phải pytest test class

    name: str
    command: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    exit_code: int | None = None
    log_ref: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.failed == 0 and self.passed > 0


class EvidenceRecord(ZeusModel):
    """Bản ghi bằng chứng chuẩn cho MỌI task — nguồn duy nhất cho learning & metric."""

    record_id: str = Field(default_factory=lambda: new_id("evr"))
    trace_id: str
    tenant_id: TenantId
    workflow_id: str | None = None
    task_id: str
    task_family: TaskFamily = TaskFamily.GENERAL
    goal: str
    input_snapshot_ref: str | None = None
    context_snapshot_ref: str | None = None
    model_provider: ProviderKind | None = None
    model_name: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None
    playbook_version: str | None = None
    route_id: str | None = None
    tools: list[str] = Field(default_factory=list)
    worker_id: str | None = None
    schedule_decision_id: str | None = None
    actions: list[ActionResult] = Field(default_factory=list)
    code_diff_ref: str | None = None
    tests: list[TestRun] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    latency_ms: int = 0
    tokens: Usage = Field(default_factory=Usage)
    cost_usd: float = Field(default=0.0, ge=0)
    retries: int = Field(default=0, ge=0)
    human_intervention: bool = False
    human_notes: str | None = None
    rollback_performed: bool = False
    rollback_ref: str | None = None
    final_outcome: Outcome = Outcome.UNVERIFIED
    verified_by: str | None = None  # judge id; phải khác hãng với bên làm nếu là model
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _claims_are_not_evidence(self) -> "EvidenceRecord":
        if self.final_outcome is Outcome.VERIFIED_SUCCESS:
            strong_ok = any(e.strength is EvidenceStrength.STRONG and e.passed is True for e in self.evidence)
            tests_ok = any(t.ok for t in self.tests)
            if not (strong_ok or tests_ok):
                raise ValueError("VERIFIED_SUCCESS requires strong passing evidence (test/build/data match/http/human)")
        return self


class RouterStat(ZeusModel):
    """Thống kê theo (family, provider, model) — metric chính: cost per verified success."""

    task_family: TaskFamily
    provider: ProviderKind
    model: str
    n: int = 0
    verified_success: int = 0
    verified_failure: int = 0
    unverified: int = 0
    total_cost_usd: float = 0.0
    total_latency_ms: int = 0

    @property
    def success_rate(self) -> float:
        verified = self.verified_success + self.verified_failure
        return self.verified_success / verified if verified else 0.0

    @property
    def cost_per_verified_success(self) -> float | None:
        return self.total_cost_usd / self.verified_success if self.verified_success else None


class DatasetRecord(ZeusModel):
    record_id: str = Field(default_factory=lambda: new_id("dsr"))
    tenant_id: TenantId = DEFAULT_TENANT
    stage: DatasetStage = DatasetStage.RAW
    task_family: TaskFamily = TaskFamily.GENERAL
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    evidence_record_id: str | None = None
    outcome: Outcome = Outcome.UNVERIFIED
    labels: dict[str, Any] = Field(default_factory=dict)
    pii_redacted: bool = False
    source: str = "runtime"
    promoted_from: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _stage_gates(self) -> "DatasetRecord":
        if self.stage in (DatasetStage.VERIFIED, DatasetStage.CURATED):
            if self.outcome is Outcome.UNVERIFIED or not self.evidence_record_id:
                raise ValueError(f"stage {self.stage.value} requires verified outcome + evidence_record_id")
        if self.stage is DatasetStage.CURATED and not self.pii_redacted:
            raise ValueError("CURATED records must be PII-redacted")
        return self


# --------------------------------------------------------------------------- memory / retrieval


class MemoryItem(ZeusModel):
    memory_id: str = Field(default_factory=lambda: new_id("mem"))
    tenant_id: TenantId = DEFAULT_TENANT
    kind: MemoryKind
    content: str
    title: str | None = None
    source_ref: str | None = None
    trust: TrustLevel = TrustLevel.UNVERIFIED
    confidence: float = Field(default=0.5, ge=0, le=1)
    tags: list[str] = Field(default_factory=list)
    valid_from: datetime = Field(default_factory=utcnow)
    valid_to: datetime | None = None
    supersedes: str | None = None
    embedding_model: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class RetrievalQuery(ZeusModel):
    tenant_id: TenantId = DEFAULT_TENANT
    text: str
    kinds: list[MemoryKind] = Field(default_factory=list)
    top_k: int = Field(default=8, ge=1, le=200)
    tags: list[str] = Field(default_factory=list)
    as_of: datetime | None = None
    require_verified: bool = False
    token_budget: int | None = Field(default=None, gt=0)


class RetrievalHit(ZeusModel):
    memory: MemoryItem
    score: float
    lexical_score: float | None = None
    vector_score: float | None = None
    rerank_score: float | None = None
    why: str | None = None


class ContextPacket(ZeusModel):
    packet_id: str = Field(default_factory=lambda: new_id("ctx"))
    tenant_id: TenantId = DEFAULT_TENANT
    task_id: str | None = None
    goal: str
    hits: list[RetrievalHit] = Field(default_factory=list)
    canonical_state: dict[str, Any] = Field(default_factory=dict)
    token_estimate: int = 0
    token_budget: int | None = None
    conflicts: list[str] = Field(default_factory=list)
    built_at: datetime = Field(default_factory=utcnow)


class HandoffPacket(ZeusModel):
    handoff_id: str = Field(default_factory=lambda: new_id("hnd"))
    tenant_id: TenantId = DEFAULT_TENANT
    task_id: str
    from_agent: str
    to_agent: str
    summary: str
    context: ContextPacket | None = None
    open_questions: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    next_action: str | None = None


# --------------------------------------------------------------------------- artifacts


class ArtifactRef(ZeusModel):
    ref: str  # vd "artifact://<tenant>/<sha256>"
    sha256: str
    size_bytes: int = Field(ge=0)
    mime: str = "application/octet-stream"
    name: str | None = None
