# MASTER STATE — ZeusVN Brain

> Cập nhật mỗi khi kết thúc một phase/merge. Nguồn sự thật về "đang ở đâu". Quyết định nằm ở `DECISION_LEDGER.md`.

| Trường | Giá trị |
|---|---|
| Cập nhật | 2026-10-04 |
| Phase | **1 — 4 workstream đã merge + TÍCH HỢP HỆ THỐNG: HOÀN TẤT (kiểm chứng trong sandbox, chưa trên host thật)** |
| Nhánh | `claude/cool-shannon-5kj313` (chưa push) |
| Kiến trúc | `docs/architecture/ZEUSVN_BRAIN_MASTER_ARCHITECTURE.md` v1.0 (khoá) |
| Contracts | `zeus/contracts` **v1.1.0** (thêm `tenant_id` tuỳ chọn cho `EvidenceStore.get`, `OutcomeRecorder.stats` — ADR-019) |
| Production | Chưa deploy gì. Không secret trong repo. Host ERP chưa bị thay đổi (gate G1/G2 vẫn đóng). |

## 1. DONE

| Hạng mục | Commit | Ghi chú |
|---|---|---|
| Phase 0: architecture lock + shared kernel | `d024130` | contracts, fakes, obs, storage/migrate, Temporal smoke, cloud_exit_check |
| WS B Project Brain / Evidence / Learning / Evals | `617504d`, merge `b373e2a` | migrations 201/202; PgMemoryStore + retriever (FTS + pgvector + RRF), PgEvidenceStore, ArtifactStore, outcome/router_stats, dataset pipeline, champion/challenger, 90 case eval |
| Sửa test shared migration (CCR #1 của A/B/C/D) | `3d55cf4` | so với `discover_migrations` thay vì `[0]` |
| WS A Control Plane | `1cd50c3`, merge `840537e` | migration 101; gateway, intent, risk, broker, planner/critic/judge, policy + tool gateway, TaskWorkflow (Temporal), Control API |
| WS C Execution Workers | `3b1bbe8`, merge `d211e28` | migration 301; registry, scheduler 10 yếu tố, PgAssignmentQueue (lease), Worker API, zeus_worker, localai, infra thin worker |
| WS D Workbench & Integrations | `010db2b`, merge `d16c447` | migration 401; Workbench `/wb` 13 trang, `/hooks` Zalo/Messenger/Shopee, outbound R2, Odoo JSON-2, domain/site |
| **Tích hợp hệ thống** | commit "ZeusVN Brain: system integration Phase 1" | xem mục 1.1 |

### 1.1 Tích hợp (integrator)

- `zeus/app/system.py` — `build_system(Settings)`: mọi store PostgreSQL thật của A/B/C/D; `PgWorkbenchDataSource` (task/approval/evidence/worker + DAG từ `task_nodes` + audit từ `audit_log`); `ControlFacade` (ingest/duyệt dùng chung cho Workbench và webhook); `ControlPlaneCompleter`, `PersistingScheduler`, `LearningOutcomeRecorder` (ADR-018); danh mục action worker `test.run`, `noop.echo`.
- `zeus/app/main.py` — `create_app(system=...)` mount `/api/v1` (A), `/worker/v1` (C), `/internal/evidence` (B), `/internal/system`, `/wb` + `/hooks/*` (D), `/healthz`, `/readyz`; Bearer token cho `/api/v1` + `/internal` (fail closed ở prod); `create_server_app()` cho uvicorn `--factory`.
- `zeus/app/worker_main.py` — Temporal worker control (TaskWorkflow trên `zeus-control`) + vòng `Dispatcher.sweep`. `zeus/app/admin.py` — phát hành/thu hồi token worker, băm mật khẩu Workbench.
- Sửa mối nối trong code workstream (tối thiểu, ADR-018): Worker API hoàn thành activity bằng `AssignmentResult` đầy đủ; Worker API lưu artifact vào ArtifactStore (B); activity dispatch (A) truyền `context` cho queue; `TemporalWorkflowControl(task_queue=)`; executor `test.run` trong zeus_worker (chỉ quảng bá khi có `test_repo`).
- `Settings` thêm `brain_config`, `workers_config`, `channels_config`, `artifact_dir`, `api_token_env`. `pyproject`: package-data (template Workbench, unit mẫu), `shellcheck-py` trong `[test]`; `scripts/_python.sh` thêm bin của venv vào PATH (test shellcheck không còn skip).
- Unit systemd + env mẫu: `zeus/app/deploy/`. Runbook: `docs/runbooks/SERVER_BOOTSTRAP.md`. ADR-018/019/020.

## 2. VERIFIED (chạy thật trong sandbox: PG16 + pgvector, Temporal CLI 1.5.1 / server 1.29.1, không Claude Cloud)

| Bằng chứng | Kết quả |
|---|---|
| `scripts/test.sh -q -p no:cacheprovider` (toàn suite) | **377 passed, 1 skipped** (skip duy nhất: `test_live_odoo_staging_smoke`, marker `live`, cần `ZEUS_RUN_LIVE=1` + Odoo thật — đúng thiết kế) |
| `scripts/cloud_exit_check.sh` (env sạch, `CLAUDE_CLOUD_AVAILABLE=false`) | BUILD PASS · TEST PASS · WORKFLOW PASS (5) · MODEL_FALLBACK PASS (5) · PROJECT_BRAIN PASS (3) · WORKER_CONTROL PASS (6) · EVIDENCE PASS (8) — **7/7 PASS** |
| `tests/shared/test_e2e_system.py::test_e2e_event_to_worker_to_evidence_to_workbench` | POST `/api/v1/events` → TaskWorkflow (Temporal thật, chạy bởi `worker_main.run`) → scheduler chọn `w-e2e` (ScheduleDecision lưu PG) → zeus_worker long-poll `/worker/v1` chạy `test.run` (pytest thật) → log artifact + kết quả → async completion → Judge PASS → EvidenceRecord VERIFIED_SUCCESS (strong, artifact link) + outcomes + dataset_records VERIFIED + nhãn outcome vào schedule_decisions → `/wb/workflows`, `/wb/workflows/<id>` (DAG SVG), `/wb/evidence/<id>`, `/wb/workers` hiển thị đúng; idempotency event; tenant khác đọc evidence = 404 |
| `test_e2e_r2_blocked_until_approval_decision` | Task deployment chờ duyệt bước R2: 0 assignment, 0 evidence, 0 node chạy trong lúc chờ; tenant khác duyệt = 404; duyệt qua Workbench (CSRF) → chạy worker → PASS, `human_intervention=true` |
| `test_e2e_r2_rejected_never_dispatches` | Từ chối qua Control API → không assignment nào, task FAILED/CANCELLED |
| `tests/shared/test_app_wiring.py` (5) | mọi router được mount; 401/503/404 đúng chỗ; prod thiếu token = 503; CLI admin; `test.run` không nhận tham số tự do, ngoài `repo_roots` bị từ chối |
| Smoke tiến trình thật (ngoài pytest) | `python -m zeus.storage.migrate` (6 migration) → `uvicorn --factory zeus.app.main:create_server_app` + `python -m zeus.app.worker_main` + `python -m zeus_worker --config worker.toml` + Temporal dev server (SQLite file) → `/readyz` ok, `/api/v1/tasks` không token = 401, ingest → task SUCCEEDED, evidence VERIFIED_SUCCESS từ `w-smoke` ("1 passed") |

## 3. FAILED

Không có test hay hạng mục Cloud Exit nào FAIL. Ghi nhận trung thực các điểm chưa đạt (không phải lỗi test):
- `docs/reports/{A,B,C,D}/REPORT.md` / `PHASE1.md` không tồn tại: subagent workstream bị chặn ghi file báo cáo. Tóm tắt Phase 1 nằm ở mục 1 và 6 của file này.
- Planner chỉ điền được tham số `goal/task_id` cho action => `http.check`, `file.checksum`, `repo.tests.run`, `db.*`, `deploy.*` chưa thể tự động hoá từ playbook; các bước đó là bước thủ công, Judge trả UNVERIFIED nếu không có bằng chứng mạnh khác.

## 4. UNVERIFIED_ON_REAL_HOST

- Mọi unit systemd (`zeus/app/deploy/*`, `zeus_worker/deploy/zeus-worker.service`, `zeus/localai/deploy/*`), `--host 10.90.30.10`, reverse proxy TLS `zeus-edge`.
- Temporal production (Temporal Server + persistence PostgreSQL); giai đoạn 1 dùng dev server SQLite — đã chạy trong sandbox, chưa trên `zeus-core`.
- `infra/may-ao` (Incus, nftables đa bridge, cloud-init, 8 instance), RAM dự phòng ERP 16 GB (G2), CPU ES2 (G3).
- Kênh thật: Zalo Bot/OA, Messenger (Graph v21.0), Shopee (chuỗi ký chưa kiểm chứng); Odoo 19 JSON-2 thật; llama.cpp/GPU `zeus-gpu`; Anthropic/OpenAI/Gemini live (không key, đúng luật).
- Sao lưu/khôi phục định kỳ trên host (pg_dump/pg_restore, artifact, temporal.db), retention (G14).
- Hiệu năng: mỗi thao tác DB mở 1 kết nối (chưa pool), chưa có index HNSW/IVFFlat cho pgvector.

## 5. ACTIVE WORK

Review round 2 (SEC-1..5, R1..R11) đã sửa — ADR-021. Chưa push.

## 6. Tóm tắt workstream Phase 1 (thay cho REPORT.md bị chặn)

| WS | Test của WS (lúc nộp) | Rủi ro còn mở chính |
|---|---|---|
| A | 142 passed (`tests/control_plane`), marker model_fallback 5, workflow 2 | idempotency action external đã bền (migration 102, ADR-021; PENDING không rõ kết quả => người vận hành xử lý); required_approvers > 1 bị TỪ CHỐI lúc nạp policy (chưa có đa người duyệt); regex PII/nguy hiểm chưa đo precision; workflow cần `workflow.patched` khi đổi cấu trúc |
| B | 34 passed (`tests/brain_learning`), oracle eval 90 case pass_rate 1.0 | chưa pool kết nối; token ước lượng thô; conflict resolver phụ thuộc tag; retention chưa duyệt (G14) |
| C | 66 passed (`tests/workers`) | lease hết hạn có thể chạy trùng (cần idempotency_key); LearnedScorer là stub; ZEUS_G1_DUYET chỉ kiểm có mặt |
| D | 86 passed, 1 skipped live (`tests/workbench_channels`) | phiên cookie ký không thu hồi từng phiên; khoá đăng nhập theo IP trong bộ nhớ; định dạng Shopee/Zalo cần đối chiếu thật |

## 7. EXACT NEXT ACTION

1. Người: đóng gate **G1/G2/G3** (số đo ERP 7 ngày, kế hoạch dry-run + rollback) rồi tạo VM `zeus-core` và chạy `docs/runbooks/SERVER_BOOTSTRAP.md` mục 2–10; ghi kết quả thật vào mục 2/4 file này.
2. Phase 2 (code, có thể làm ngay trong repo): planner sinh tham số typed action từ Intent entities (url/domain/order_id) để bật `http.check`/`file.checksum` trong playbook; pool kết nối Postgres; index pgvector; Temporal Server + Postgres persistence; `ModelRequest` có risk/complexity (CCR A-3).
3. Kênh: đối chiếu Zalo Bot/OA, Messenger, Shopee với tài khoản thật trên staging (marker `live`), rồi mới bật `enabled`.
4. Gate **G12** (API key + trần chi tiêu) trước khi bật cloud LLM; G14 trước khi bật retention.

## 8. Gates đang mở (chờ người)

G1 cài Incus/nftables trên host ERP · G2 số đo RAM/CPU ERP · G3 CPU ES2 · G8 Windows GUI · G12 API key + trần chi tiêu · G14 retention.
(Danh sách đầy đủ: mục 10 kiến trúc.)

## 9. Interfaces

Contracts v1.1.0 = v1.0.0 + `EvidenceStore.get(record_id, tenant_id=None)`, `OutcomeRecorder.stats(task_family=None, tenant_id=None)`.
Danh sách enum/model/Protocol/API/DB như v1.0.0 (xem `zeus/contracts`, ADR-009..019). Migrations: 000 shared, 101–102 A, 201–202 B, 301 C, 401 D. `Task.untrusted` (ADR-021, cộng thêm).
