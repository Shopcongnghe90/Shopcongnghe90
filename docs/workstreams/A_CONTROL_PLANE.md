# Workstream A — CONTROL PLANE

## OBJECTIVE
Xây bộ điều khiển trung tâm: Event Gateway → Intent → Risk → Planner/Critic/Judge → Model Broker → Policy + Typed Tool
Gateway + Approval → Temporal TaskWorkflow (DAG, saga, approval signal, dispatch activity) → Control API.
Kết quả Phase 1: một sự kiện Workbench/API chạy hết vòng đời tới `Verdict` trên Temporal thật, với fallback model khi tắt cloud.

## OWNED PATHS
- `zeus/gateway/**`, `zeus/intent/**`, `zeus/risk/**`, `zeus/broker/**`, `zeus/planning/**` (planner/critic/judge),
  `zeus/policy/**` (policy engine, tool gateway, approval store, budget, PII redaction), `zeus/orchestration/**`
  (TaskWorkflow, activities phía control, DAG runner, saga), `zeus/api/**` (Control API router `/api/v1/*`)
- `config/models.yaml`, `config/policy.yaml`
- `migrations/1xx_*.sql` (bắt đầu 101)
- `tests/control_plane/**` (có `__init__.py`), `docs/reports/A/**`

## DO NOT TOUCH
Mọi shared path (`zeus/contracts/**`, `zeus/obs/**`, `zeus/config.py`, `zeus/storage/**`, `zeus/testing/**`, `zeus/app/**`,
`migrations/0xx_*`, `tests/conftest.py`, `tests/shared/**`, `scripts/**`, `docs/architecture|state|workstreams/**`, `pyproject.toml`)
và owned paths của B/C/D. Cần đổi contract ⇒ CONTRACT_CHANGE_REQUEST trong báo cáo, không sửa.

## INTERFACES
- **Triển khai:** `ModelProvider` (`AnthropicProvider` qua SDK `anthropic`; `OpenAICompatibleProvider` cho OpenAI/Gemini/local),
  `ModelBroker`, `IntentEngine`, `RiskEngine`, `Planner`, `Critic`, `Judge`, `PolicyEngine`, `ToolGateway`, `ApprovalStore`.
- **Tiêu thụ (qua Protocol, test bằng fake):** `BrainRetriever`, `EvidenceStore`, `OutcomeRecorder` (B); `WorkerRegistry`,
  `Scheduler`, `AssignmentQueue` (C); `ToolProvider`, `ChannelAdapter` (D).
- **Kiểu:** `Event`, `Intent`, `RiskAssessment`, `Task`, `TaskGraph`, `Plan`, `Critique`, `Verdict`, `ModelRequest/Response`,
  `RouteDecision`, `ActionSpec`, `TypedAction`, `ActionResult`, `PolicyDecision`, `ApprovalRequest/Decision`, `TaskAssignment`.
- **API:** `Paths.EVENTS`, `TASKS`, `TASK`, `TASK_EVIDENCE`, `TASK_CANCEL`, `APPROVALS`, `APPROVAL_DECISION`, `EVIDENCE`,
  `WORKERS`, `ROUTER_STATS`; task queue `TASK_QUEUE_CONTROL`.
- Router xuất ra: `zeus.api.router: fastapi.APIRouter` (integrator include vào `create_app`).

## DEPENDENCIES
- Shared kernel Phase 0 (đã có). Temporal CLI `/opt/zeus/bin/temporal`, PG16 (fixture `pg_dsn`).
- SDK `anthropic` (đã cài), `httpx` cho OpenAI-compatible. Không gọi mạng thật trong test (dùng `httpx.MockTransport`; test thật = marker `live`).
- B/C/D chỉ qua Protocol + `zeus.testing.fakes`.

## ACCEPTANCE
1. Broker: route theo prior + lọc PII/capability/budget/`verified_model_id`; fallback chain; hết provider ⇒ `ProviderUnavailable` ⇒ workflow retry/xếp hàng; cost tính từ `config/models.yaml`.
2. Anthropic adapter map đúng usage/stop_reason/lỗi (rate limit/overloaded ⇒ `ProviderUnavailable`); OpenAI-compatible adapter chạy được với llama.cpp, OpenAI, Gemini chỉ đổi base_url/key env.
3. Policy: R0/R1 allow, R2/R3 approval, budget, denylist; mọi decision có `rule_ids`.
4. Tool Gateway: validate JSON Schema args, tenant check, policy, approval, idempotency key cho external, ghi `audit_log`.
5. TaskWorkflow: DAG song song, approval bằng signal (timeout ⇒ EXPIRED), dispatch qua activity async completion, saga compensation, luôn hội tụ về `Verdict`.
6. Critic/Judge khác hãng bên làm (assert trong code); Judge deterministic trước.
7. Untrusted content không bao giờ vào system prompt (test).

## TESTS
`tests/control_plane/`: unit (broker/policy/risk/intent/gateway với fakes), adapter tests với `httpx.MockTransport` và
Anthropic client mock, workflow tests trên `temporal_env` (marker `temporal`), store tests (marker `pg`).
Bắt buộc: test marker `cloud_exit_model_fallback` (cloud tắt ⇒ local hoàn thành; local tắt ⇒ xếp hàng, không mất task).

## EVIDENCE
Dòng tổng kết `scripts/test.sh` thật; output `scripts/cloud_exit_check.sh`; một trace end-to-end (span tree) xuất từ `trace_spans`;
bảng route decisions mẫu.

## MERGE CONTRACT
Chỉ sửa owned paths; toàn bộ test PASS (không skip pg/temporal); cloud_exit_check không FAIL và MODEL_FALLBACK = PASS;
không import implementation của B/C/D (chỉ `zeus.contracts`); `docs/reports/A/PHASE1.md` đủ mục; CONTRACT_CHANGE_REQUEST (nếu có) ghi rõ diff đề xuất.
