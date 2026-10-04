# MASTER STATE — ZeusVN Brain

> Cập nhật mỗi khi kết thúc một phase/merge. Nguồn sự thật về "đang ở đâu". Quyết định nằm ở `DECISION_LEDGER.md`.

| Trường | Giá trị |
|---|---|
| Cập nhật | 2026-10-04 |
| Phase | **0 — ARCHITECTURE LOCK + SHARED KERNEL: HOÀN TẤT** |
| Nhánh | `claude/cool-shannon-5kj313` |
| Kiến trúc | `docs/architecture/ZEUSVN_BRAIN_MASTER_ARCHITECTURE.md` v1.0 (khoá) |
| Contracts | `zeus/contracts` v1.0.0 (FROZEN) |
| Production | Chưa deploy gì. Không có secret production trong repo. Host ERP chưa bị thay đổi. |

## 1. Trạng thái hiện tại (đã kiểm chứng bằng chạy thật)

| Hạng mục | Trạng thái | Bằng chứng |
|---|---|---|
| Shared contracts (models, 22 Protocol, Control/Worker API) | XONG | `tests/shared/test_contracts.py` |
| Fakes in-memory cho mọi Protocol | XONG | `tests/shared/test_fakes.py` (isinstance + pipeline end-to-end) |
| Observability (trace ctx, JSON log, span OTel, PgSpanExporter) | XONG | `tests/shared/test_obs.py`, `test_migrations.py::test_pg_span_exporter` |
| Config (env, không secret mặc định, CLAUDE_CLOUD_AVAILABLE=false) | XONG | `tests/shared/test_config_app.py` |
| Storage + migration runner + `000_core.sql` | XONG | `tests/shared/test_migrations.py` (PG16 thật + pgvector) |
| Temporal smoke (dev server local, pydantic converter) | XONG | `tests/shared/test_temporal_smoke.py` |
| App factory `/healthz` | XONG | `tests/shared/test_config_app.py::test_healthz` |
| Cloud exit check | BUILD/TEST/WORKFLOW = PASS; MODEL_FALLBACK/PROJECT_BRAIN/WORKER_CONTROL/EVIDENCE = NOT_YET | `scripts/cloud_exit_check.sh` |
| `infra/may-ao` | Thiết kế cũ, CHƯA refactor (việc của C) | bảng reconcile mục 5 kiến trúc |

Lệnh tái lập: `scripts/test.sh` (toàn bộ, gồm `pg` + `temporal`) và `scripts/cloud_exit_check.sh`.

## 2. ACTIVE WORK

Không có workstream nào đang chạy. Phase 1 sẵn sàng khởi động song song 4 workstream:

| WS | Tài liệu | Trạng thái |
|---|---|---|
| A Control Plane | `docs/workstreams/A_CONTROL_PLANE.md` | READY |
| B Project Brain & Learning | `docs/workstreams/B_PROJECT_BRAIN_LEARNING.md` | READY |
| C Execution Workers | `docs/workstreams/C_EXECUTION_WORKERS.md` | READY |
| D Workbench & Integrations | `docs/workstreams/D_WORKBENCH_INTEGRATIONS.md` | READY |

## 3. EXACT NEXT ACTION

1. Khởi động 4 workstream Phase 1 song song, mỗi WS trên nhánh riêng từ commit Phase 0, chỉ sửa owned paths,
   test bằng `zeus.testing.fakes` cho phần phụ thuộc WS khác.
2. Mỗi WS nộp `docs/reports/<WS>/PHASE1.md` (commit, file, dòng tổng kết pytest thật, evidence, CONTRACT_CHANGE_REQUEST, rủi ro)
   và thêm test marker Cloud Exit của mình (A: `cloud_exit_model_fallback`; B: `cloud_exit_project_brain`, `cloud_exit_evidence`;
   C: `cloud_exit_worker_control`).
3. Integrator merge B → C → A → D, nối router vào `zeus/app/main.py`, chạy `scripts/cloud_exit_check.sh` ⇒ mục tiêu 7/7 PASS.
4. Song song (người): thu số đo ERP production 7 ngày (gate G2), kiểm chữ ký CPU/microcode (G3). Không cài gì lên host trước G1.

## 4. Gates đang mở (chờ người)

G1 cài Incus/nftables trên host ERP · G2 số đo RAM/CPU ERP · G3 CPU ES2 · G12 API key + trần chi tiêu.
(Danh sách đầy đủ: mục 10 kiến trúc.)

## 5. Interfaces đã freeze (v1.0.0)

- **Enums:** Channel, EventKind, EventStage, TaskFamily (18 + general), RiskLevel (R0–R3), TaskStatus, Outcome, DatasetStage,
  ProviderKind, ModelRole, RouteStrategy, PolicyEffect, ApprovalStatus, VerdictDecision, Severity, WorkerKind, WorkerStatus,
  MemoryKind, TrustLevel, EvidenceKind, EvidenceStrength.
- **Models:** TenantId, TraceContext, ChannelIdentity, Attachment, Event, Intent, RiskAssessment, ActionSpec, TypedAction,
  ActionResult, PolicyDecision, ApprovalRequest, ApprovalDecision, Task, TaskNode, TaskGraph, RouteCandidate, RouteDecision,
  Plan, CritiqueIssue, Critique, Verdict, ChatMessage, ModelRequest, Usage, Cost, ModelResponse, ProviderCapability,
  WorkerCapability, GpuInfo, Inventory, WorkerInfo, WorkerHeartbeat, ScheduleFeatures, ScheduleCandidate, ScheduleDecision,
  EvidenceItem, TestRun, EvidenceRecord, RouterStat, DatasetRecord, MemoryItem, RetrievalQuery, RetrievalHit, ContextPacket,
  HandoffPacket, ArtifactRef.
- **Protocols:** ModelProvider, ModelBroker, IntentEngine, RiskEngine, Planner, Critic, Judge, PolicyEngine, ToolProvider,
  ToolGateway, ApprovalStore, EvidenceStore, MemoryStore, EmbeddingProvider, Reranker, BrainRetriever, OutcomeRecorder,
  WorkerRegistry, Scheduler, AssignmentQueue, ChannelAdapter, WorkbenchDataSource (+ ProviderUnavailable, PolicyDenied, ApprovalRequired).
- **API:** `Paths` (Control `/api/v1/*`, Worker `/worker/v1/*`, hooks `/hooks/*`, `/wb`), headers, task queues
  `zeus-control`/`zeus-brain`/`zeus-dispatch`, schema EventIngest*, TaskSummary, TaskListResponse, ApprovalListResponse,
  ErrorBody, WorkerRegister*, HeartbeatResponse, PollRequest/Response, TaskAssignment, AssignmentResult, CancelAck, Ack.
- **DB:** `000_core.sql` — tenants, audit_log (append-only), trace_spans, schema_migrations.
