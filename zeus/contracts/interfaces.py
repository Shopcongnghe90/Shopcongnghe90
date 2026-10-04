"""Protocol interfaces (FROZEN v1.0 — Phase 0).

Mỗi Protocol có chủ sở hữu triển khai (workstream) ghi trong docstring. Bên tiêu thụ chỉ phụ thuộc
Protocol này, không import implementation của workstream khác. Fake in-memory: ``zeus.testing.fakes``.
Tất cả I/O là async; hàm thuần tính toán (policy evaluate, channel verify) là sync.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from zeus.contracts.api import AssignmentResult, TaskAssignment
from zeus.contracts.models import (
    ActionResult,
    ActionSpec,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    Channel,
    ContextPacket,
    Critique,
    DatasetRecord,
    Event,
    EvidenceRecord,
    Intent,
    MemoryItem,
    MemoryKind,
    ModelRequest,
    ModelResponse,
    Plan,
    PolicyDecision,
    ProviderCapability,
    ProviderKind,
    RetrievalHit,
    RetrievalQuery,
    RiskAssessment,
    RouteDecision,
    RouterStat,
    ScheduleDecision,
    Task,
    TaskFamily,
    TaskNode,
    TaskStatus,
    TypedAction,
    Verdict,
    WorkerHeartbeat,
    WorkerInfo,
    WorkerStatus,
)

# ============================================================== A — control plane


class ProviderUnavailable(RuntimeError):
    """Provider không dùng được (thiếu key, mất mạng, hết quota, CLAUDE_CLOUD/cloud bị tắt...).

    Broker bắt lỗi này để fallback sang provider kế tiếp hoặc xếp hàng.
    """


class PolicyDenied(PermissionError):
    pass


class ApprovalRequired(PermissionError):
    def __init__(self, request: ApprovalRequest):
        super().__init__(f"approval required: {request.approval_id}")
        self.request = request


@runtime_checkable
class ModelProvider(Protocol):
    """Adapter 1 hãng/1 runtime. Owner A (zeus/broker): AnthropicProvider (SDK chính thức),
    OpenAICompatibleProvider (OpenAI / Gemini OpenAI-compat / llama.cpp local)."""

    kind: ProviderKind

    def capabilities(self) -> list[ProviderCapability]: ...

    async def complete(self, request: ModelRequest, model: str) -> ModelResponse: ...

    async def health(self) -> bool: ...


@runtime_checkable
class ModelBroker(Protocol):
    """Provider-neutral broker. Owner A. route() thuần quyết định; complete() thực thi có fallback."""

    def route(self, request: ModelRequest) -> RouteDecision: ...

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


@runtime_checkable
class IntentEngine(Protocol):
    """Owner A (zeus/intent)."""

    async def classify(self, event: Event) -> Intent: ...


@runtime_checkable
class RiskEngine(Protocol):
    """Owner A (zeus/risk)."""

    async def assess(self, event: Event, intent: Intent) -> RiskAssessment: ...

    def assess_action(self, action: TypedAction, spec: ActionSpec) -> RiskAssessment: ...


@runtime_checkable
class Planner(Protocol):
    """Owner A (zeus/planning). Sinh Plan + TaskGraph (DAG hợp lệ)."""

    async def plan(self, task: Task, context: ContextPacket) -> Plan: ...


@runtime_checkable
class Critic(Protocol):
    """Owner A. Bên soát phải khác hãng với bên làm; đúng 1 vòng."""

    async def critique(self, plan: Plan, context: ContextPacket) -> Critique: ...


@runtime_checkable
class Judge(Protocol):
    """Owner A. Kết luận dựa trên EvidenceRecord (test/build/so khớp), không dựa lời khẳng định."""

    async def judge(self, task: Task, evidence: Sequence[EvidenceRecord]) -> Verdict: ...


@runtime_checkable
class PolicyEngine(Protocol):
    """Owner A (zeus/policy). Thuần, deterministic, đọc config/policy.yaml."""

    def evaluate(self, action: TypedAction, spec: ActionSpec, task: Task | None = None) -> PolicyDecision: ...


@runtime_checkable
class ToolProvider(Protocol):
    """Nhà cung cấp typed actions (Owner: workstream sở hữu tích hợp — D cho ERP/kênh/domain,
    C cho hạ tầng). Chỉ được gọi thông qua ToolGateway."""

    def specs(self) -> list[ActionSpec]: ...

    async def execute(self, action: TypedAction, spec: ActionSpec) -> ActionResult: ...


@runtime_checkable
class ToolGateway(Protocol):
    """Owner A. Cửa duy nhất cho side-effect: validate schema -> policy -> approval -> execute -> evidence.
    Ném PolicyDenied / ApprovalRequired thay vì thực thi."""

    def list_actions(self) -> list[ActionSpec]: ...

    async def execute(self, action: TypedAction, task: Task | None = None) -> ActionResult: ...


@runtime_checkable
class ApprovalStore(Protocol):
    """Owner A (lưu trữ 1xx). Workbench (D) đọc/ghi quyết định qua Control API."""

    async def request(self, req: ApprovalRequest) -> ApprovalRequest: ...

    async def decide(self, decision: ApprovalDecision) -> ApprovalRequest: ...

    async def get(self, approval_id: str) -> ApprovalRequest | None: ...

    async def list(self, tenant_id: str, status: ApprovalStatus | None = None) -> list[ApprovalRequest]: ...


# ============================================================== B — brain / evidence / learning


@runtime_checkable
class EvidenceStore(Protocol):
    """Owner B (zeus/evidence). Append-only."""

    async def put(self, record: EvidenceRecord) -> str: ...

    async def get(self, record_id: str, tenant_id: str | None = None) -> EvidenceRecord | None:
        """tenant_id (v1.1.0): khi có, chỉ trả bản ghi của tenant đó (cô lập tenant)."""
        ...

    async def list_for_task(self, tenant_id: str, task_id: str) -> list[EvidenceRecord]: ...


@runtime_checkable
class MemoryStore(Protocol):
    """Owner B (zeus/brain)."""

    async def put(self, item: MemoryItem) -> str: ...

    async def get(self, tenant_id: str, memory_id: str) -> MemoryItem | None: ...

    async def list(self, tenant_id: str, kind: MemoryKind | None = None, limit: int = 100) -> list[MemoryItem]: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Owner B (interface impl), runtime do C (GPU local) — /v1/embeddings OpenAI-compatible."""

    model_name: str
    dim: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@runtime_checkable
class Reranker(Protocol):
    """Owner B."""

    async def rerank(self, query: str, hits: Sequence[RetrievalHit], top_k: int) -> list[RetrievalHit]: ...


@runtime_checkable
class BrainRetriever(Protocol):
    """Owner B. Hybrid retrieval (lexical + vector + rerank) + context budgeter."""

    async def retrieve(self, query: RetrievalQuery) -> list[RetrievalHit]: ...

    async def build_context(self, query: RetrievalQuery, task: Task | None = None) -> ContextPacket: ...


@runtime_checkable
class OutcomeRecorder(Protocol):
    """Owner B (zeus/learning). Biến EvidenceRecord thành DatasetRecord + router stats."""

    async def record(self, evidence: EvidenceRecord) -> DatasetRecord: ...

    async def stats(self, task_family: TaskFamily | None = None, tenant_id: str | None = None) -> list[RouterStat]:
        """tenant_id (v1.1.0): None => tenant mặc định của recorder."""
        ...


# ============================================================== C — workers


@runtime_checkable
class WorkerRegistry(Protocol):
    """Owner C (zeus/workers)."""

    async def register(self, info: WorkerInfo) -> WorkerInfo: ...

    async def heartbeat(self, hb: WorkerHeartbeat) -> None: ...

    async def get(self, worker_id: str) -> WorkerInfo | None: ...

    async def list(self, status: WorkerStatus | None = None) -> list[WorkerInfo]: ...

    async def last_heartbeat(self, worker_id: str) -> WorkerHeartbeat | None: ...

    async def mark_stale(self, now: datetime, ttl_s: int) -> list[str]: ...


@runtime_checkable
class Scheduler(Protocol):
    """Owner C. Bootstrap deterministic (10 yếu tố). Trả ScheduleDecision có feature vector."""

    async def schedule(
        self,
        task: Task,
        node: TaskNode,
        workers: Sequence[WorkerInfo],
        heartbeats: Mapping[str, WorkerHeartbeat],
    ) -> ScheduleDecision: ...


@runtime_checkable
class AssignmentQueue(Protocol):
    """Owner C (bảng 3xx). Cầu nối Temporal <-> thin worker (ADR-011):
    activity dispatch (A) gọi enqueue(..., task_token) rồi raise CompleteAsync; Worker API (C) gọi
    complete() khi worker POST kết quả, nhận lại task_token để hoàn thành activity bất đồng bộ."""

    async def enqueue(self, assignment: TaskAssignment, worker_id: str, task_token: bytes | None = None) -> None: ...

    async def poll(self, worker_id: str, max_assignments: int = 1) -> list[TaskAssignment]: ...

    async def complete(self, result: AssignmentResult) -> bytes | None: ...

    async def cancel(self, assignment_id: str) -> None: ...

    async def pending_cancellations(self, worker_id: str) -> list[str]: ...


# ============================================================== D — workbench / channels


@runtime_checkable
class ChannelAdapter(Protocol):
    """Owner D (zeus/channels). verify() thuần; normalize() sinh Event untrusted.
    Outbound chỉ qua ToolProvider/typed action có approval — adapter KHÔNG tự gửi."""

    channel: Channel

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool: ...

    def normalize(self, headers: Mapping[str, str], body: bytes) -> list[Event]: ...

    def outbound_specs(self) -> list[ActionSpec]: ...


@runtime_checkable
class WorkbenchDataSource(Protocol):
    """Read model cho Workbench (Owner D tiêu thụ; integrator nối vào A/B/C stores)."""

    async def list_tasks(self, tenant_id: str, status: TaskStatus | None = None, limit: int = 50) -> list[Task]: ...

    async def get_task(self, tenant_id: str, task_id: str) -> Task | None: ...

    async def list_approvals(self, tenant_id: str, status: ApprovalStatus | None = None) -> list[ApprovalRequest]: ...

    async def list_workers(self) -> list[WorkerInfo]: ...

    async def list_evidence(self, tenant_id: str, task_id: str) -> list[EvidenceRecord]: ...


ALL_PROTOCOLS: tuple[type, ...] = (
    ModelProvider,
    ModelBroker,
    IntentEngine,
    RiskEngine,
    Planner,
    Critic,
    Judge,
    PolicyEngine,
    ToolProvider,
    ToolGateway,
    ApprovalStore,
    EvidenceStore,
    MemoryStore,
    EmbeddingProvider,
    Reranker,
    BrainRetriever,
    OutcomeRecorder,
    WorkerRegistry,
    Scheduler,
    AssignmentQueue,
    ChannelAdapter,
    WorkbenchDataSource,
)
