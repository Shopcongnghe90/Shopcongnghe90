# Workstream C — EXECUTION WORKERS

## OBJECTIVE
Xây lớp thực thi: Worker Registry + Worker API (server), Resource Scheduler deterministic 10 yếu tố, AssignmentQueue (lease,
cancel, async completion), gói `zeus_worker` thin worker chạy trên VM, local AI runtime (`zeus/localai`), và refactor
`infra/may-ao` sang mô hình thin worker (sửa lỗi remote-control/setup-token).

## OWNED PATHS
- `zeus/workers/**` (registry, heartbeat, inventory, scheduler, assignment queue, Worker API router `/worker/v1/*`,
  dispatch helper hoàn thành activity bằng task_token)
- `zeus_worker/**` (thin worker: register/heartbeat/poll/execute typed executors/cancel/timeout/upload result+log+artifact+evidence)
- `zeus/localai/**` (cấu hình + health + benchmark llama.cpp server, embedding, OCR runtime)
- `infra/**`, `config/workers.yaml`
- `migrations/3xx_*.sql` (bắt đầu 301)
- `tests/workers/**` (có `__init__.py`), `docs/reports/C/**`

## DO NOT TOUCH
Shared paths và owned paths A/B/D. Đổi contract ⇒ CONTRACT_CHANGE_REQUEST. Không chạy bất kỳ lệnh nào lên host thật
(Incus/nftables/sysctl) — chỉ script + `--chay-thu` + tài liệu (gate G1).

## INTERFACES
- **Triển khai:** `WorkerRegistry`, `Scheduler`, `AssignmentQueue`.
- **Kiểu:** `WorkerInfo`, `WorkerCapability`, `Inventory`, `GpuInfo`, `WorkerHeartbeat`, `ScheduleFeatures`, `ScheduleCandidate`,
  `ScheduleDecision`, `TaskAssignment`, `AssignmentResult`, `CancelAck`, `HeartbeatResponse`, `PollRequest/Response`,
  `WorkerRegisterRequest/Response`, `ActionResult`, `EvidenceItem`, `ArtifactRef`.
- **API:** `Paths.WORKER_*`, header `HEADER_WORKER_TOKEN`, task queue `TASK_QUEUE_DISPATCH`.
- Router xuất ra: `zeus.workers.api.router: fastapi.APIRouter`.
- `zeus_worker` chỉ phụ thuộc `zeus.contracts` + `httpx` (không import `zeus.workers`, không Temporal SDK, không API key provider).

## DEPENDENCIES
PG16 (`pg_dsn`), Temporal (`temporal_env`) cho test async completion, `000_core.sql`. A tiêu thụ queue/scheduler qua Protocol.

## ACCEPTANCE
1. Register bằng token riêng; heartbeat; `mark_stale` ⇒ OFFLINE ⇒ assignment hết lease được trả lại hàng đợi.
2. Scheduler deterministic, ràng buộc cứng (capability, tenant scope, status, network zone), lưu feature vector + weights + candidates; không ai đủ ⇒ queued.
3. AssignmentQueue: lease, idempotent complete (kết quả lặp không hoàn thành activity 2 lần), chỉ worker sở hữu được complete, cancel lan tới worker qua heartbeat.
4. `zeus_worker`: executors typed (tối thiểu `repo.tests.run`, `shell.allowlisted`, `http.check`), timeout, cancel, upload log/artifact, gửi `TestRun`/`EvidenceItem` thật.
5. Async completion thật trên Temporal: activity chờ ⇒ worker POST result ⇒ workflow nhận `ActionResult`.
6. `infra/`: bỏ `claude-remote-control` + `CLAUDE_CODE_OAUTH_TOKEN`; cloud-init cài `zeus_worker` systemd; profile 8 instance theo bảng kiến trúc mục 4; `zeus-gpu` container `nvidia.runtime`; nftables đa bridge; mọi script giữ `--chay-thu`.
7. `zeus/localai`: health + benchmark script (tok/s, VRAM) xuất evidence; hệ thống chạy khi GPU vắng.

## TESTS
`tests/workers/` (marker `pg`, `temporal`). Bắt buộc marker `cloud_exit_worker_control` (register/heartbeat/poll/result/cancel
qua Worker API thật bằng TestClient/uvicorn + PG + Temporal async completion). Test tĩnh: `infra/**` không còn `setup-token`/`remote-control` trong đường chạy mặc định.

## EVIDENCE
Dòng tổng kết pytest thật; output cloud_exit_check; output `--chay-thu` của script infra; mẫu `ScheduleDecision` JSON.

## MERGE CONTRACT
Chỉ owned paths; test PASS (không skip); WORKER_CONTROL = PASS; không có thay đổi host thật; `docs/reports/C/PHASE1.md` đủ mục.
