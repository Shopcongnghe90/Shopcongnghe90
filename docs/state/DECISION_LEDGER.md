# DECISION LEDGER — ZeusVN Brain

**Append-only.** Không sửa/xoá ADR đã ghi. Thay đổi quyết định = ADR mới có trường `Thay thế: ADR-xxx`
và đổi trạng thái ADR cũ bằng một dòng ADR mới (không sửa dòng cũ).
Trạng thái: `ACCEPTED` · `PROPOSED` · `SUPERSEDED` · `GATED` (chờ người quyết).

---

## ADR-001 — Temporal là durable workflow backbone
- **Ngày:** 2026-10-04
- **Quyết định:** Mọi luồng nhiều bước (task, approval chờ dài, dispatch worker, retry) chạy trên Temporal (Python SDK `temporalio`, Server 1.29.1 qua CLI 1.5.1). Data converter `pydantic_data_converter`.
- **Lý do:** Quyết định đã chốt của người dùng; repo chưa có engine khác; cần retry/timeout/signal/tiếp tục sau crash mà không tự viết state machine.
- **Bằng chứng:** `tests/shared/test_temporal_smoke.py` chạy workflow + 2 activity trên dev server local (`/opt/zeus/bin/temporal`), contract pydantic đi qua Temporal — PASS (Phase 0).
- **Trạng thái:** ACCEPTED

## ADR-002 — NATS hoãn cho tới khi có bằng chứng cần pub/sub
- **Ngày:** 2026-10-04
- **Quyết định:** Không đưa NATS/broker message vào Phase 0–1. Ingress → Temporal start workflow trực tiếp; worker long-poll Worker API.
- **Lý do:** Quyết định đã chốt; giảm thành phần vận hành trên host chung ERP.
- **Bằng chứng:** Chưa có yêu cầu fan-out; điều kiện mở lại: số đo cho thấy cần fan-out > 1 consumer/event hoặc độ trễ long-poll vượt SLO.
- **Trạng thái:** ACCEPTED

## ADR-003 — PostgreSQL 16 + pgvector là kho dữ liệu duy nhất
- **Ngày:** 2026-10-04
- **Quyết định:** Brain, evidence, control, worker, audit, trace và persistence của Temporal dùng PostgreSQL 16 (DB riêng cho Temporal); vector bằng pgvector; lexical bằng FTS.
- **Lý do:** Một hệ quản trị để backup/restore/giám sát; hybrid retrieval không cần vector DB riêng; đã có sẵn binary + pgvector.
- **Bằng chứng:** `tests/shared/test_migrations.py` dựng cluster PG16 thật, `CREATE EXTENSION vector`, áp `000_core.sql` — PASS.
- **Trạng thái:** ACCEPTED

## ADR-004 — Thin worker thay cho "1 Claude / 1 VM"
- **Ngày:** 2026-10-04
- **Quyết định:** VM chỉ chạy `zeus_worker` (register/heartbeat/poll/execute typed executors/cancel/timeout/upload). Không cài LLM, không giữ API key provider/ERP trên VM. Ưu tiên API chính thức; GUI chỉ khi bắt buộc (gate G8).
- **Lý do:** Chi phí, bảo mật (key tập trung 1 chỗ), dễ học (scheduler/broker thấy mọi lời gọi), tránh rủi ro khoá tài khoản cá nhân.
- **Bằng chứng:** Reconcile `infra/may-ao` ở mục 5 kiến trúc; contract Worker API trong `zeus/contracts/api.py`.
- **Trạng thái:** ACCEPTED

## ADR-005 — Model Broker trung lập nhà cung cấp
- **Ngày:** 2026-10-04
- **Quyết định:** Hai adapter: (1) `OpenAICompatibleProvider` dùng chung cho OpenAI API, Gemini (endpoint OpenAI-compatible) và llama.cpp server local (`/v1/chat/completions`, `/v1/embeddings`); (2) `AnthropicProvider` dùng SDK chính thức `anthropic`. Runtime chỉ dùng API key (Anthropic Console, OpenAI API, Gemini API) hoặc local; gói thuê bao (Claude Max, ChatGPT Plus, Google AI Pro) không dùng cho runtime. Model ID OpenAI/Gemini để trong config, đánh dấu "cần điền" cho tới khi kiểm chứng.
- **Lý do:** Gói thuê bao không kèm API; một adapter chuẩn giảm code; Anthropic SDK có typed errors/usage.
- **Bằng chứng:** Protocol `ModelProvider`/`ModelBroker` + fake fallback test (`test_broker_falls_back_and_respects_pii`) — PASS. Giá Anthropic (USD/1M vào/ra): opus-5-5 4/20, sonnet-5-5 2/10, haiku-4-5 1/5, fable-5-1 10/50; Batch −50%.
- **Trạng thái:** ACCEPTED

## ADR-006 — Workbench không có build step frontend
- **Ngày:** 2026-10-04
- **Quyết định:** FastAPI + Jinja2 + vanilla JS/CSS vendored trong repo, không CDN, không Node build. Tiếng Việt.
- **Lý do:** Chạy offline trên server Ubuntu, không chuỗi cung ứng npm, ít thành phần.
- **Bằng chứng:** App factory `zeus/app/main.py` + test `/healthz` — PASS.
- **Trạng thái:** ACCEPTED

## ADR-007 — GPU: giữ 1× RTX 3060 12GB, local qua container Incus
- **Ngày:** 2026-10-04
- **Quyết định:** 1 model thường trú Qwen3.5-9B GGUF Q4_K_M qua llama.cpp server + embedding nhỏ + OCR, trong container Incus `nvidia.runtime` (không passthrough VM). Không mua 3060 thứ 2; 3090 chỉ khi số đo đòi model > 12GB (gate G9). Hệ thống phải chạy khi không có GPU.
- **Lý do:** VRAM 12GB đủ cho 9B Q4 + embedding; container chia GPU nhẹ hơn passthrough.
- **Bằng chứng:** Số đo cộng đồng ~55 tok/s decode (chưa đo trên máy — việc của C Phase 2).
- **Trạng thái:** ACCEPTED

## ADR-008 — Chính sách kênh
- **Ngày:** 2026-10-04
- **Quyết định:** Zalo Bot Platform là kênh chính; Zalo OA Open API có; Messenger Platform cho Fanpage (24h tự động, sau đó HUMAN_AGENT do người); Shopee: đơn qua Open Platform push (HMAC-SHA256), chat dùng Trợ lý Chat AI có sẵn, chat API chỉ khi whitelist; **Facebook cá nhân: không tự động hoá**. Outbound mọi kênh = typed action có approval.
- **Lý do:** Điều khoản nền tảng, rủi ro khoá tài khoản, chữ ký webhook có kiểm chứng được.
- **Bằng chứng:** Tài liệu nền tảng (dữ kiện đã kiểm chứng trong brief Phase 0); `ChannelAdapter` contract + fake verify test — PASS.
- **Trạng thái:** ACCEPTED

## ADR-009 — Đánh số migration theo sở hữu
- **Ngày:** 2026-10-04
- **Quyết định:** `migrations/NNN_<ws>_<mô_tả>.sql`; 0xx shared, 1xx A, 2xx B, 3xx C, 4xx D. Bất biến sau khi áp dụng (checksum). FK chéo workstream chỉ trỏ vào bảng 0xx.
- **Lý do:** 4 workstream song song không đụng số; thứ tự áp dụng xác định.
- **Bằng chứng:** `zeus/storage/migrate.py` + test checksum mismatch, migration lỗi rollback, idempotent — PASS.
- **Trạng thái:** ACCEPTED

## ADR-010 — Cloud Exit
- **Ngày:** 2026-10-04
- **Quyết định:** `CLAUDE_CLOUD_AVAILABLE=false` mặc định. Runtime (`zeus/`, `zeus_worker/`) cấm tham chiếu claude.ai, Routines, remote-control, setup-token, `/tmp/claude`, `CLAUDE_CODE_*`. `scripts/cloud_exit_check.sh` phải không FAIL ở mọi commit; cuối Phase 1 tất cả 7 hạng mục PASS.
- **Lý do:** Claude Cloud chỉ là xưởng build tạm; hệ thống phải build/test/chạy độc lập trên server Ubuntu.
- **Bằng chứng:** Phase 0: BUILD PASS, TEST PASS, WORKFLOW PASS, 4 hạng mục NOT_YET (chưa có code). Kiểm thử âm: chèn "claude.ai" vào `zeus_worker` ⇒ BUILD FAIL.
- **Trạng thái:** ACCEPTED

## ADR-011 — Dispatch worker qua Worker API + async activity completion
- **Ngày:** 2026-10-04
- **Quyết định:** Worker không chạy Temporal SDK. Activity dispatch (control) chọn worker, `AssignmentQueue.enqueue(assignment, worker_id, task_token)`, raise `CompleteAsync`; worker long-poll `POST /worker/v1/poll`, gửi `AssignmentResult`; Worker API hoàn thành activity bằng task_token. Lease + idempotency key cho retry.
- **Lý do:** Worker mỏng (kể cả Windows nếu có), một bề mặt xác thực (token mỗi worker), firewall đơn giản (worker chỉ cần tới 1 cổng HTTP), Temporal không lộ ra mạng agent.
- **Bằng chứng:** Contract `AssignmentQueue` + `InMemoryAssignmentQueue` test (ownership, idempotent complete) — PASS. Test Temporal thật thuộc C Phase 1.
- **Trạng thái:** ACCEPTED

## ADR-012 — Gate cho mọi thay đổi host ERP production
- **Ngày:** 2026-10-04
- **Quyết định:** Cài Incus/ZFS/nftables/sysctl/kernel module, tạo VM, đổi mạng trên host đang chạy ERP production là **HIGH PRODUCTION RISK** ⇒ human decision gate (G1, G2, G3). Không script nào trong repo tự chạy trên host mà không có `--chay-thu` + approval.
- **Lý do:** ERP production chạy trên chính host; CPU ES2 không bảo hành.
- **Bằng chứng:** Bảng gates mục 10 kiến trúc.
- **Trạng thái:** GATED

## ADR-013 — Resource Scheduler bootstrap deterministic, lưu feature vector
- **Ngày:** 2026-10-04
- **Quyết định:** Scorer tuyến tính trên 10 yếu tố (CapabilityMatch, WorkerHealth, CPUAvailable, RAMAvailable, QueueLoad, HistoricalSuccess, DataLocality, Urgency, Risk, FailurePenalty) + ràng buộc cứng; mọi `ScheduleDecision` lưu features/weights/candidates/policy_version và sau này outcome ⇒ dữ liệu học P(success | task, worker, state).
- **Lý do:** Quyết định đã chốt; deterministic để test/giải thích được trước khi học.
- **Bằng chứng:** `ScheduleFeatures`/`ScheduleDecision` contract; `FakeScheduler` test — PASS.
- **Trạng thái:** ACCEPTED

## ADR-014 — Điều chỉnh owned paths so với gợi ý
- **Ngày:** 2026-10-04
- **Quyết định:** (1) `zeus/integrations/**` (Odoo JSON-2, SaaS/site, domain) giao **D** — tích hợp ngoài đi cùng kênh; A chỉ sở hữu gateway/policy tiêu thụ chúng qua `ToolProvider`. (2) Router Worker API nằm trong `zeus/workers` (**C**), không ở `zeus/api` (A chỉ Control API). (3) Thêm Protocol `ToolProvider` và `AssignmentQueue` vào contracts (cần cho ranh giới A↔D và A↔C). (4) Config tách file theo owner: `models.yaml`, `policy.yaml` (A), `brain.yaml` (B), `workers.yaml` (C), `channels.yaml` (D). (5) Báo cáo workstream ở `docs/reports/<WS>/` (owned bởi WS). (6) `tests/__init__.py` shared; mỗi thư mục test workstream có `__init__.py`.
- **Lý do:** Tránh hai workstream sửa cùng file; ranh giới phụ thuộc rõ.
- **Bằng chứng:** `docs/workstreams/*.md`, mục 11 kiến trúc.
- **Trạng thái:** ACCEPTED

## ADR-015 — Verified outcome là đơn vị sự thật; metric cost per verified success
- **Ngày:** 2026-10-04
- **Quyết định:** `EvidenceRecord` VERIFIED_SUCCESS bắt buộc bằng chứng mạnh (test/build/data match/HTTP/xác nhận người); `Verdict` PASS ⇒ VERIFIED_SUCCESS + evidence_ids; `DatasetRecord` VERIFIED/CURATED cần evidence, CURATED cần PII-redacted. Router/scheduler chỉ học từ outcome đã kiểm chứng.
- **Lý do:** "Claims are not evidence; execution is not completion."
- **Bằng chứng:** Validator trong `zeus/contracts/models.py`, test `test_claims_are_not_evidence`, `test_dataset_stage_gates` — PASS.
- **Trạng thái:** ACCEPTED

## ADR-016 — Gỡ Claude Code Remote Control khỏi runtime (sửa lỗi thiết kế cũ)
- **Ngày:** 2026-10-04
- **Quyết định:** Bỏ dịch vụ `claude-remote-control` và `CLAUDE_CODE_OAUTH_TOKEN`/`setup-token` khỏi ảnh VM. Remote Control (nếu người muốn) chỉ cài tay trên máy bàn làm việc, đăng nhập bằng `claude auth login`, không phải thành phần hệ thống.
- **Lý do:** Remote Control không nhận setup-token/API key; là dependency cloud; vi phạm ADR-010.
- **Bằng chứng:** Lỗi đã biết trong brief; C sửa `infra/**` ở Phase 1.
- **Trạng thái:** ACCEPTED

## ADR-017 — PII và cloud LLM theo tenant
- **Ngày:** 2026-10-04
- **Quyết định:** `tenants.allow_cloud_llm` mặc định `false`; tenant nội bộ `zeusvn` = `true` nhưng PII vẫn bị che trước khi gửi cloud. Request `contains_pii` không redact được ⇒ chỉ local hoặc xếp hàng.
- **Lý do:** Luật BVDLCN 91/2025/QH15 hiệu lực 01/01/2026.
- **Bằng chứng:** `migrations/000_core.sql`; fake broker test PII → local — PASS.
- **Trạng thái:** ACCEPTED

## ADR-018 — Mối nối tích hợp Phase 1 (A↔C↔B↔D) và kiểu hoàn thành activity dispatch
- **Ngày:** 2026-10-04
- **Bổ sung:** ADR-011 (không thay thế).
- **Quyết định:**
  1. Dispatch tới worker trong hệ thống tích hợp đi qua activity `zeus.control.run_node` của TaskWorkflow (A), kiểu trả về là `AssignmentResult` (không phải `ActionResult`). Worker API (C) hoàn thành activity bằng `ControlPlaneCompleter.complete_assignment(task_token, AssignmentResult)` — giữ nguyên evidence/log/artifact của worker và gắn `metrics.schedule_decision_id` từ bảng `assignments`. Đường huỷ/hết lease của C (chỉ có `ActionResult`) được bọc thành `AssignmentResult`. `DispatchActivities` (queue `zeus-dispatch`) của C giữ làm thành phần độc lập, không đăng ký trong worker tích hợp.
  2. `ScheduleDecision` của mọi lần dispatch được lưu (`PersistingScheduler`); outcome đã kiểm chứng ghi ngược vào quyết định (`LearningOutcomeRecorder`) => nhãn học P(success | task, worker).
  3. Activity dispatch của A truyền `context` (family/urgency/risk/capability) vào `PgAssignmentQueue.enqueue` để giao lại đúng capability khi worker mất tín hiệu.
  4. Artifact upload của worker đi vào `ArtifactStore` (B) khi được cấu hình, để `EvidenceRecord` tham chiếu `artifact://<tenant>/<sha>` hợp lệ (trước đó PgEvidenceStore ném ArtifactMissing).
  5. Từ vựng action của playbook ↔ executor của thin worker: thêm executor `test.run` (bộ test cố định do người vận hành cấu hình trên worker, bỏ qua tham số tự do). Control chỉ đăng ký action worker mà planner điền được tham số (`test.run`, `noop.echo`), capability `action:<tên>` do worker tự quảng bá.
- **Lý do:** Các workstream test bằng fakes nên không lộ ra 4 lỗ hở này; tích hợp thật bị gãy ở cả 4 điểm (kiểu activity, mất evidence, thiếu artifact, không có executor cho action của playbook).
- **Bằng chứng:** `tests/shared/test_e2e_system.py` (PG16 + Temporal thật + zeus_worker trong tiến trình) — 3 PASS; smoke tiến trình thật (uvicorn `create_server_app` + `zeus.app.worker_main` + `python -m zeus_worker` + Temporal dev server + PG16): task SUCCEEDED, evidence VERIFIED_SUCCESS từ worker.
- **Trạng thái:** ACCEPTED

## ADR-019 — Contracts v1.1.0 và xử lý CONTRACT_CHANGE_REQUEST Phase 1
- **Ngày:** 2026-10-04
- **Quyết định:**
  - ÁP DỤNG: `tests/shared/test_migrations.py` so với `discover_migrations` (đã làm ở 3d55cf4); `EvidenceStore.get(record_id, tenant_id=None)` và `OutcomeRecorder.stats(task_family=None, tenant_id=None)` (thay đổi cộng thêm, tương thích ngược) => `CONTRACTS_VERSION = 1.1.0`; Control API truyền tenant khi đọc evidence/stats; `Settings` thêm `brain_config`, `workers_config`, `channels_config`, `artifact_dir`, `api_token_env` (CCR C-2).
  - GHI NHẬN, KHÔNG ĐỔI CONTRACT: `WorkbenchDataSource` thêm get_graph/last_heartbeat/list_audit (CCR D-2) — cài trong `PgWorkbenchDataSource` và tiêm qua `WorkbenchContext.graph_provider/audit_provider/registry`, không thêm vào Protocol (tránh phá fakes/isinstance). `ModelRequest` thêm risk/complexity/freshness (CCR A-3) và `RouterStat.prompt_version` (B) — hoãn sang Phase 2 khi router học dùng tới. `AssignmentQueue.enqueue` có `context` (C-3) — giữ tuỳ chọn ngoài Protocol, A kiểm tra chữ ký.
  - Từ cấm `routine(s)` trong `zeus/` (B) giữ nguyên trong `scripts/cloud_exit_forbidden.txt`.
- **Lý do:** Chỉ đổi contract khi cần cho an toàn (cô lập tenant) hoặc tích hợp; phần còn lại không chặn Phase 1.
- **Bằng chứng:** toàn suite PASS; `test_e2e_system.py` kiểm tenant khác đọc evidence => 404.
- **Trạng thái:** ACCEPTED

## ADR-020 — Hình dạng triển khai Phase 1 trên zeus-core và xác thực API
- **Ngày:** 2026-10-04
- **Quyết định:** Hai tiến trình Python trên `zeus-core`: (1) `uvicorn --factory zeus.app.main:create_server_app` phục vụ `/api/v1` (A), `/worker/v1` (C), `/internal/evidence` + `/internal/system` (B/integrator), `/wb` (D), `/hooks/*` (D), `/healthz`, `/readyz`; (2) `python -m zeus.app.worker_main` chạy TaskWorkflow trên queue `zeus-control` + vòng bảo trì dispatcher (sweep). `/api/v1` và `/internal` yêu cầu `Authorization: Bearer` từ `ZEUS_API_TOKEN`; env `staging/prod` thiếu token => 503 (fail closed). Temporal giai đoạn 1 = dev server CLI với persistence SQLite trên đĩa; mục tiêu production = Temporal Server + Postgres persistence (chưa kiểm chứng). Unit systemd mẫu ở `zeus/app/deploy/`.
- **Lý do:** Ít thành phần nhất chạy được không cần Claude Cloud; Control API trước đây không có xác thực (rủi ro A).
- **Bằng chứng:** `tests/shared/test_app_wiring.py` (401/503/404 đúng chỗ, CLI phát hành token); smoke tiến trình thật ở ADR-018.
- **Trạng thái:** ACCEPTED (cài trên host thật: GATED theo ADR-012, G1/G2)
