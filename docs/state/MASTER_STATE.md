# MASTER STATE — ZeusVN Brain

> Cập nhật mỗi khi kết thúc một phase/merge. Nguồn sự thật về "đang ở đâu". Quyết định nằm ở `DECISION_LEDGER.md`.

| Trường | Giá trị |
|---|---|
| Cập nhật | 2026-10-04 (review cuối + handoff Cloud Exit) |
| Phase | **1 hoàn tất (kiểm chứng trong sandbox, chưa trên host thật) + Phase 2 vòng 1: tham số typed từ Intent, pool Postgres, HNSW, UX Workbench vòng 2** |
| Nhánh | `claude/cool-shannon-5kj313` (chưa push) |
| Kiến trúc | `docs/architecture/ZEUSVN_BRAIN_MASTER_ARCHITECTURE.md` v1.0 (khoá) |
| Contracts | `zeus/contracts` **v1.1.0** (thêm `tenant_id` tuỳ chọn cho `EvidenceStore.get`, `OutcomeRecorder.stats` — ADR-019; cộng thêm `Task.untrusted` ADR-021 và `Task.entities` ADR-022, trường mới có mặc định) |
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
| Review round 2: SEC-1..5, R1..R11 | `0c4b4dd` | ADR-021 (idempotency action external bền, migration 102, đã gồm trong đây) |
| Review cuối: SEC-7, SEC-8 | commit "final review fixes + Cloud Exit handoff" | ADR-023; `tests/shared/test_final_review.py` |
| **Phase 2 vòng 1 + UX vòng 2** | commit "ZeusVN Brain: Phase 2 gaps + UX fixes (round 2)" | xem mục 1.2, ADR-022 |

### 1.1 Tích hợp (integrator)

- `zeus/app/system.py` — `build_system(Settings)`: mọi store PostgreSQL thật của A/B/C/D; `PgWorkbenchDataSource` (task/approval/evidence/worker + DAG từ `task_nodes` + audit từ `audit_log`); `ControlFacade` (ingest/duyệt dùng chung cho Workbench và webhook); `ControlPlaneCompleter`, `PersistingScheduler`, `LearningOutcomeRecorder` (ADR-018); danh mục action worker `test.run`, `noop.echo`.
- `zeus/app/main.py` — `create_app(system=...)` mount `/api/v1` (A), `/worker/v1` (C), `/internal/evidence` (B), `/internal/system`, `/wb` + `/hooks/*` (D), `/healthz`, `/readyz`; Bearer token cho `/api/v1` + `/internal` (fail closed ở prod); `create_server_app()` cho uvicorn `--factory`.
- `zeus/app/worker_main.py` — Temporal worker control (TaskWorkflow trên `zeus-control`) + vòng `Dispatcher.sweep`. `zeus/app/admin.py` — phát hành/thu hồi token worker, băm mật khẩu Workbench.
- Sửa mối nối trong code workstream (tối thiểu, ADR-018): Worker API hoàn thành activity bằng `AssignmentResult` đầy đủ; Worker API lưu artifact vào ArtifactStore (B); activity dispatch (A) truyền `context` cho queue; `TemporalWorkflowControl(task_queue=)`; executor `test.run` trong zeus_worker (chỉ quảng bá khi có `test_repo`).
- `Settings` thêm `brain_config`, `workers_config`, `channels_config`, `artifact_dir`, `api_token_env`. `pyproject`: package-data (template Workbench, unit mẫu), `shellcheck-py` trong `[test]`; `scripts/_python.sh` thêm bin của venv vào PATH (test shellcheck không còn skip).
- Unit systemd + env mẫu: `zeus/app/deploy/`. Runbook: `docs/runbooks/SERVER_BOOTSTRAP.md`. ADR-018/019/020.

### 1.2 Phase 2 vòng 1 (ADR-022)

- **Tham số typed từ Intent:** `Task.entities` (url/domain/order_id/path/sha256, điền ở `EventGateway` và `classify_and_assess`) -> `typed_args` trong planner chỉ sinh `http.check`/`file.checksum`/`repo.tests.run` (và `erp.sale_order.read` theo order_id nếu Odoo đã phát hành) khi qua `ParamPolicy` (`zeus/policy/params.py`, allowlist `params:` trong `config/policy.yaml`). Policy Engine kiểm lại (`P-PARAM-ALLOWLIST`); model plan không được bịa tham số; worker kiểm theo cấu hình riêng. Playbook-2 có bước `optional` (http.check sau test, file.checksum khi deploy) chỉ xuất hiện khi sinh được tham số. **Allowlist mặc định rỗng => hành vi cũ (bước thủ công)**: người vận hành phải điền `params:` và cấu hình worker để bật.
- **Idempotency action external:** đã có từ ADR-021 (migration 102, `PgIdempotency`); thêm test gọi đồng thời cùng khoá và khoá suy ra từ nội dung.
- **Pool Postgres async:** `zeus/storage/pool.py` (tự viết vì `psycopg_pool` không có trong venv/dependency), nối qua `aconnect` nên mọi store dùng chung; `ZEUS_DB_POOL_MAX` (mặc định 10, 0 = tắt); API lifespan và `worker_main.run` mở pool qua `System.open_pool()`; `/internal/system` trả `db_pool`.
- **HNSW pgvector:** migration `203_b_vector_hnsw.sql` (index biểu thức một phần theo số chiều 256) + `PgBrainRetriever` dùng dạng truy vấn khớp index + `PgMemoryStore.ensure_hnsw_index(dim)`.
- **UX Workbench (UX-01..08):** duyệt hết hạn báo đúng; audit quyết định duyệt (Workbench và Control API); PRG + nonce cho /wb/command; thẻ duyệt R2/R3 nổi bật + xác nhận; bảng dạng thẻ trên điện thoại; worker mất tín hiệu; tiếng Việt thống nhất; tương phản WCAG AA hai theme.

## 2. VERIFIED (chạy thật trong sandbox: PG16 + pgvector, Temporal CLI 1.5.1 / server 1.29.1, không Claude Cloud)

| Bằng chứng | Kết quả |
|---|---|
| `/opt/zeus/venv/bin/python -m pytest -q` (toàn suite, sau review cuối, 2026-10-04) | **482 passed, 2 skipped** (skip: `test_live_odoo_staging_smoke` marker `live`; `shellcheck` khi gọi pytest trực tiếp không có trên PATH — chạy qua `scripts/test.sh`/`cloud_exit_check.sh` thì chạy thật). Trước Phase 2: 377 passed (commit `749b4dc`) |
| `scripts/cloud_exit_check.sh` (env sạch, `CLAUDE_CLOUD_AVAILABLE=false`) | BUILD PASS · TEST PASS (483 passed, 1 skipped) · WORKFLOW PASS (6) · MODEL_FALLBACK PASS (5) · PROJECT_BRAIN PASS (3) · WORKER_CONTROL PASS (7) · EVIDENCE PASS (9) — **7/7 PASS** |
| `tests/shared/test_e2e_system.py::test_e2e_event_to_worker_to_evidence_to_workbench` | POST `/api/v1/events` → TaskWorkflow (Temporal thật, chạy bởi `worker_main.run`) → scheduler chọn `w-e2e` (ScheduleDecision lưu PG) → zeus_worker long-poll `/worker/v1` chạy `test.run` (pytest thật) → log artifact + kết quả → async completion → Judge PASS → EvidenceRecord VERIFIED_SUCCESS (strong, artifact link) + outcomes + dataset_records VERIFIED + nhãn outcome vào schedule_decisions → `/wb/workflows`, `/wb/workflows/<id>` (DAG SVG), `/wb/evidence/<id>`, `/wb/workers` hiển thị đúng; idempotency event; tenant khác đọc evidence = 404 |
| `test_e2e_r2_blocked_until_approval_decision` | Task deployment chờ duyệt bước R2: 0 assignment, 0 evidence, 0 node chạy trong lúc chờ; tenant khác duyệt = 404; duyệt qua Workbench (CSRF) → chạy worker → PASS, `human_intervention=true` |
| `test_e2e_r2_rejected_never_dispatches` | Từ chối qua Control API → không assignment nào, task FAILED/CANCELLED |
| Phase 2 vòng 1 (PG16 + pgvector + Temporal thật, không mạng ngoài) | `test_typed_params.py` (34: allowlist url/path/sha, entity Intent, planner sinh http.check/file.checksum/repo.tests.run, bước optional nối lại DAG, model không bịa tham số, Policy Engine DENY ngoài allowlist); e2e `test_e2e_planner_typed_params_http_check_runs_on_worker` (URL trong yêu cầu -> `http.check` chạy trên thin worker thật với transport giả -> evidence HTTP_CHECK passed; URL ngoài allowlist -> không dispatch); `test_db_pool_hnsw.py` (12: tái dùng kết nối, trần + timeout, commit/rollback, kết nối chết/huỷ bị loại, 60 thao tác store song song <= 4 kết nối, index HNSW từ migration, EXPLAIN dùng HNSW, kết quả khớp quét chính xác, chiều khác không làm hỏng insert); `test_phase2_idempotency.py` (2) |
| UX Workbench vòng 2 | `test_ux_round2.py` (17 test; chạy trên code cũ: 17 FAIL; sau sửa: PASS) gồm 3 test PG thật (duyệt hết hạn -> EXPIRED, audit_log, PRG/nonce không tạo task thứ hai); ảnh chụp mobile 390px bằng Chromium headless kiểm tay (duyệt, workers) |
| `tests/shared/test_app_wiring.py` (5) | mọi router được mount; 401/503/404 đúng chỗ; prod thiếu token = 503; CLI admin; `test.run` không nhận tham số tự do, ngoài `repo_roots` bị từ chối |
| Smoke tiến trình thật (ngoài pytest) | `python -m zeus.storage.migrate` (6 migration) → `uvicorn --factory zeus.app.main:create_server_app` + `python -m zeus.app.worker_main` + `python -m zeus_worker --config worker.toml` + Temporal dev server (SQLite file) → `/readyz` ok, `/api/v1/tasks` không token = 401, ingest → task SUCCEEDED, evidence VERIFIED_SUCCESS từ `w-smoke` ("1 passed") |

## 3. FAILED

Không có test hay hạng mục Cloud Exit nào FAIL. Ghi nhận trung thực các điểm chưa đạt (không phải lỗi test):
- `docs/reports/{A,B,C,D}/REPORT.md` / `PHASE1.md` không tồn tại: subagent workstream bị chặn ghi file báo cáo. Tóm tắt Phase 1 nằm ở mục 1 và 6 của file này.
- Planner đã điền được tham số typed cho `http.check`/`file.checksum`/`repo.tests.run` (ADR-022) nhưng CHỈ khi allowlist `params:` (control) và cấu hình executor (worker) được điền; `db.*`, `deploy.*`, `dns.*`, `code.apply_patch` vẫn chưa có provider/tham số => bước thủ công, Judge trả UNVERIFIED nếu không có bằng chứng mạnh khác.
- Số chiều embedding khác 256 cần gọi `PgMemoryStore.ensure_hnsw_index(dim)` (migration 203 chỉ tạo index 256 chiều); chưa đo recall/độ trễ HNSW trên dữ liệu thật (chỉ so khớp kết quả với quét chính xác trên bảng nhỏ).

## 4. UNVERIFIED_ON_REAL_HOST

- Mọi unit systemd (`zeus/app/deploy/*`, `zeus_worker/deploy/zeus-worker.service`, `zeus/localai/deploy/*`), `--host 10.90.30.10`, reverse proxy TLS `zeus-edge`.
- Temporal production (Temporal Server + persistence PostgreSQL); giai đoạn 1 dùng dev server SQLite — đã chạy trong sandbox, chưa trên `zeus-core`.
- `infra/may-ao` (Incus, nftables đa bridge, cloud-init, 8 instance), RAM dự phòng ERP 16 GB (G2), CPU ES2 (G3).
- Kênh thật: Zalo Bot/OA, Messenger (Graph v21.0), Shopee (chuỗi ký chưa kiểm chứng); Odoo 19 JSON-2 thật; llama.cpp/GPU `zeus-gpu`; Anthropic/OpenAI/Gemini live (không key, đúng luật).
- Sao lưu/khôi phục định kỳ trên host (pg_dump/pg_restore, artifact, temporal.db), retention (G14).
- Hiệu năng: pool kết nối và index HNSW đã có (ADR-022) nhưng CHƯA đo trên tải/dữ liệu thật (kích thước pool, `ef_search`, recall); `max_connections` của Postgres trên `zeus-core` chưa đối chiếu với `ZEUS_DB_POOL_MAX` x số tiến trình.

## 5. ACTIVE WORK

Không còn. Mọi việc đã commit trên nhánh `claude/cool-shannon-5kj313` (chưa push).

### 5.1 SECURITY_FINDINGS

Đã sửa (có test hồi quy): SEC-1 (lộ token trong log), SEC-2 (cổng duyệt theo rủi ro task), SEC-3 (task untrusted chỉ tự chạy `*.read/get/list/search/check`), SEC-4 (backup), SEC-5 (`required_approvers > 1` bị từ chối), SEC-7 (login tên non-ASCII không còn TypeError, so sánh theo bytes UTF-8), SEC-8 (API dev không token: chỉ nhận từ loopback hoặc khi đặt rõ `ZEUS_ALLOW_NO_TOKEN=1`, ngược lại 401; staging/prod vẫn 503 fail closed). Review cuối: 0 finding; quét secret sạch trên cây và toàn lịch sử.

Còn mở (known gaps / open items):

| ID | Mức | File | Lý do để lại |
|---|---|---|---|
| SEC-6 | low | `zeus/channels` (Shopee push) | Push thiếu timestamp vẫn nhận; chuỗi ký Shopee chưa đối chiếu tài khoản thật, sửa sau khi có `live` test |
| R12 | low | `zeus/workers/registry.py` | Heartbeat ghi đè trạng thái DRAINING; ảnh hưởng nhỏ, cần quyết định hành vi drain |
| UX-09 | low | `zeus/workbench/router.py` | Lỗi trả JSON thô thay vì trang lỗi; chỉ thẩm mỹ |
| UX-10 | low | `zeus/workbench` (chi phí) | Ngày tính theo UTC, chưa theo múi giờ VN |
| UX-11 | low | template Workbench | Menu điện thoại vẫn nhận Tab, thiếu skip-link (a11y) |
| UX-12 | low | trang Brain | Thiếu trạng thái hướng dẫn khi rỗng |
| Runbook §7 | info | `docs/runbooks/SERVER_BOOTSTRAP.md` dòng ~94 | Comment liệt kê migration thiếu 102 và 203; runner vẫn áp dụng đủ |

Ghi chú SEC-8: dev sau reverse proxy cùng máy sẽ thấy client là loopback; mọi triển khai thật phải đặt `ZEUS_API_TOKEN` và `ZEUS_ENV=staging|prod`.

### 5.2 CLOUD_EXIT_STATUS

| Hạng mục | Kết quả |
|---|---|
| BUILD | PASS |
| TEST | PASS (483 passed, 1 skipped) |
| WORKFLOW | PASS (6) |
| MODEL_FALLBACK | PASS (5) |
| PROJECT_BRAIN | PASS (3) |
| WORKER_CONTROL | PASS (7) |
| EVIDENCE | PASS (9) |

Điều kiện: source đã commit đầy đủ (người dùng push lên remote tự quản — CHƯA push từ phiên này) · không secret nào chỉ tồn tại trên cloud (quét secret sạch) · không phụ thuộc runtime vào Claude Cloud (audit grep sạch; Anthropic chỉ là provider tuỳ chọn, mặc định không key) · build path, bootstrap, migration, rollback, test suite đều có tài liệu (`docs/runbooks/SERVER_BOOTSTRAP.md` §2-12, `scripts/test.sh`, `scripts/cloud_exit_check.sh`).

## 6. Tóm tắt workstream Phase 1 (thay cho REPORT.md bị chặn)

| WS | Test của WS (lúc nộp) | Rủi ro còn mở chính |
|---|---|---|
| A | 142 passed (`tests/control_plane`) lúc nộp; hiện thêm test_security_round2/reliability_round2/typed_params/phase2_idempotency; marker model_fallback 5, workflow 2 | idempotency action external đã bền (migration 102, ADR-021; PENDING không rõ kết quả => người vận hành xử lý); required_approvers > 1 bị TỪ CHỐI lúc nạp policy (chưa có đa người duyệt); regex PII/nguy hiểm chưa đo precision; workflow cần `workflow.patched` khi đổi cấu trúc |
| B | 34 passed (`tests/brain_learning`) lúc nộp, oracle eval 90 case pass_rate 1.0 | pool + HNSW đã có (ADR-022, chưa đo trên dữ liệu thật); token ước lượng thô; conflict resolver phụ thuộc tag; retention chưa duyệt (G14) |
| C | 66 passed (`tests/workers`) | lease hết hạn có thể chạy trùng (cần idempotency_key); LearnedScorer là stub; ZEUS_G1_DUYET chỉ kiểm có mặt |
| D | 86 passed, 1 skipped live (`tests/workbench_channels`) | phiên cookie ký không thu hồi từng phiên; khoá đăng nhập theo IP trong bộ nhớ; định dạng Shopee/Zalo cần đối chiếu thật |

## 7. EXACT_SERVER_NEXT_ACTION

Chạy trên host ERP (chỉ đọc cho tới khi G1 đóng), ghi kết quả vào mục 4 file này:

1. G3 (CPU/microcode, chỉ đọc): `lscpu | grep -E 'Model name|Flags' ; grep -m1 -E 'microcode|model name' /proc/cpuinfo ; dmesg 2>/dev/null | grep -i microcode`
2. G2 (RAM ERP, đo 7 ngày): `free -m ; vmstat 60 5 ; ps -eo rss,comm --sort=-rss | head -15` (lặp bằng cron/sar trong 7 ngày; cần RAM dự phòng >= 16 GB sau đỉnh ERP).
3. G1: có số đo G2/G3 + kế hoạch dry-run và rollback được người duyệt thì mới cài Incus/nftables (`infra/may-ao`).
4. Tạo VM `zeus-core` theo `infra/may-ao` và `docs/runbooks/SERVER_BOOTSTRAP.md`: §2 gói hệ thống, §3 Postgres 16 + pgvector, §4 Temporal CLI (sha256), §5 clone + venv `/opt/zeus/venv` + `pip install`, §6 secret (`openssl rand`, `python -m zeus.app.admin hash-password`), §7 `pg_dump` rồi `python -m zeus.storage.migrate` (kiểm `--status`), §8-9 systemd + thin worker.
5. Kiểm sau cài (§10): `scripts/test.sh` và `scripts/cloud_exit_check.sh` phải xanh; `curl /readyz`; đặt `ZEUS_ENV=prod` và `ZEUS_API_TOKEN`.
6. Sau đó: kênh `live` trên staging, G12 trước cloud LLM, G14 trước retention; Phase 2 còn lại như danh sách cũ (Temporal Server + PG persistence, đa người duyệt, provider `db.*`/`deploy.*`/`dns.*`, đo HNSW/pool).

## 8. Gates đang mở (chờ người)

G1 cài Incus/nftables trên host ERP · G2 số đo RAM/CPU ERP · G3 CPU ES2 · G8 Windows GUI · G12 API key + trần chi tiêu · G14 retention.
(Danh sách đầy đủ: mục 10 kiến trúc.)

## 9. Interfaces

Contracts v1.1.0 = v1.0.0 + `EvidenceStore.get(record_id, tenant_id=None)`, `OutcomeRecorder.stats(task_family=None, tenant_id=None)`.
Danh sách enum/model/Protocol/API/DB như v1.0.0 (xem `zeus/contracts`, ADR-009..019). Migrations: 000 shared, 101–102 A, 201–203 B, 301 C, 401 D. `Task.untrusted` (ADR-021) và `Task.entities` (ADR-022) cộng thêm, mặc định rỗng/False.
