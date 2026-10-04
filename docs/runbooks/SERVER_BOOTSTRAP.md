# RUNBOOK — Dựng ZeusVN Brain trên Ubuntu (không cần Claude Cloud)

> Phạm vi: máy `zeus-core` (VM Ubuntu 24.04 theo kiến trúc mục 4) hoặc một máy Ubuntu **không** chạy ERP production.
> Kết quả: Control API + Worker API + Workbench + webhook + Temporal worker control chạy bằng systemd, Postgres 16 + pgvector,
> Temporal; thin worker trên các VM agent. `CLAUDE_CLOUD_AVAILABLE=false` — không có bước nào cần Claude Cloud.

## 0. GATE trước khi làm (bắt buộc đọc)

| Tình huống | Quy định |
|---|---|
| Máy đích là **host đang chạy ERP production** (cài gói hệ thống, Postgres/Temporal, mở cổng, đổi firewall, tạo VM) | **HIGH PRODUCTION RISK — DỪNG.** Cần gate **G1** (kế hoạch + dry-run + rollback + cửa sổ bảo trì + backup ERP đã restore thử) và **G2** (số đo RAM/CPU/IO ERP 7 ngày). ADR-012. Không chạy runbook này trên host ERP khi chưa có quyết định ghi vào `docs/state/DECISION_LEDGER.md`. |
| Máy đích là VM `zeus-core` đã được tạo sau G1/G2 | Làm theo runbook. |
| Máy dev/test riêng | Làm theo runbook, hoặc chỉ `scripts/dev_bootstrap.sh` + `scripts/test.sh`. |

Không bao giờ: dùng chung cluster Postgres của ERP production, cho bridge agent tới ERP production, đặt secret vào git.

## 1. Thành phần và cổng

| Thành phần | Lệnh / unit | Bind |
|---|---|---|
| PostgreSQL 16 + pgvector | `postgresql@16-main` | 127.0.0.1:5432 |
| Temporal (giai đoạn 1: dev server, persistence SQLite trên đĩa) | `zeus-temporal.service` | 127.0.0.1:7233 (UI 127.0.0.1:8233) |
| API: `/api/v1` (A), `/worker/v1` (C), `/wb` (D), `/hooks/*` (D), `/internal/*`, `/healthz`, `/readyz` | `zeus-api.service` (uvicorn `--factory zeus.app.main:create_server_app`) | 10.90.30.10:8080 (`br-core`) |
| Temporal worker control (TaskWorkflow + vòng bảo trì dispatcher) | `zeus-control-worker.service` (`python -m zeus.app.worker_main`) | — |
| Thin worker (trên mỗi VM agent) | `zeus-worker.service` (`python -m zeus_worker --config /etc/zeus-worker/worker.toml`) | ra ngoài tới `zeus-core:8080/worker/v1/*` |

Unit mẫu: `zeus/app/deploy/` (API, control worker, Temporal, `zeus.env.sample`) và `zeus_worker/deploy/` (worker).

## 2. Gói hệ thống (zeus-core)

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git curl postgresql-16 postgresql-16-pgvector
python3 --version            # cần >= 3.11 (Ubuntu 24.04: 3.12)
sudo useradd --system --home /var/lib/zeus --shell /usr/sbin/nologin zeus
sudo install -d -o zeus -g zeus -m 0750 /var/lib/zeus /var/lib/zeus/artifacts /var/lib/zeus/temporal
sudo install -d -o root -g zeus -m 0750 /etc/zeus
sudo install -d -o root -g root -m 0755 /opt/zeus /opt/zeus/bin
```

## 3. PostgreSQL 16 + pgvector

```bash
sudo -u postgres psql -c "CREATE ROLE zeus LOGIN"
sudo -u postgres psql -c "CREATE DATABASE zeus OWNER zeus ENCODING 'UTF8'"
sudo -u postgres psql -d zeus -c "CREATE EXTENSION IF NOT EXISTS vector"   # cần superuser; migration 000 chỉ thử lại
```
Xác thực: `peer` qua socket cho user `zeus` (DSN `host=/var/run/postgresql dbname=zeus user=zeus`) hoặc mật khẩu trong
`~/.pgpass`/`/etc/zeus/zeus.env` (0600). Không mở Postgres ra ngoài 127.0.0.1.

## 4. Temporal

```bash
# Tải Temporal CLI 1.5.x (server 1.29.x) từ https://github.com/temporalio/cli/releases (bản linux_amd64), kiểm sha256 trong
# trang phát hành, rồi:
sudo install -m 0755 temporal /opt/zeus/bin/temporal
/opt/zeus/bin/temporal --version
sudo install -m 0644 /opt/zeus/app/zeus/app/deploy/zeus-temporal.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now zeus-temporal
```
Giai đoạn 1 dùng `temporal server start-dev --db-filename /var/lib/zeus/temporal/temporal.db` (workflow bền qua restart).
Mục tiêu production: Temporal Server với persistence PostgreSQL (kiến trúc 3.10) — **chưa kiểm chứng trên host thật**.

## 5. Mã nguồn + môi trường Python

```bash
sudo git clone <kho nội bộ>/Shopcongnghe90.git /opt/zeus/app        # hoặc giải nén bản phát hành đã ký
sudo python3 -m venv /opt/zeus/venv
sudo /opt/zeus/venv/bin/pip install --upgrade pip
sudo /opt/zeus/venv/bin/pip install /opt/zeus/app                    # gói gồm template Workbench + unit mẫu
```
`config/`, `migrations/`, `docs/state/DECISION_LEDGER.md` được đọc từ `/opt/zeus/app` (WorkingDirectory của unit).

## 6. Cấu hình `/etc/zeus/zeus.env`

```bash
sudo install -m 0600 -o root -g zeus /opt/zeus/app/zeus/app/deploy/zeus.env.sample /etc/zeus/zeus.env
sudo chmod 0640 /etc/zeus/zeus.env
openssl rand -hex 32          # -> ZEUS_API_TOKEN (Bearer cho /api/v1, /internal; BẮT BUỘC khi ZEUS_ENV=prod)
openssl rand -hex 32          # -> ZEUS_WB_SESSION_KEY
/opt/zeus/venv/bin/python -m zeus.app.admin hash-password   # -> ZEUS_WB_PASSWORD_HASH (mật khẩu >= 12 ký tự)
sudoedit /etc/zeus/zeus.env
```
Mặc định an toàn: không key cloud => intent/planner/critic/judge chạy deterministic; `ZEUS_LOCAL_LLM_URL` trỏ tới
llama.cpp trên `zeus-gpu` khi có. Secret kênh thiếu => webhook kênh đó trả 404. Cloud LLM chỉ bật sau gate G12.

## 7. Migration

```bash
set -a; . /etc/zeus/zeus.env; set +a
cd /opt/zeus/app
sudo -u postgres pg_dump -Fc zeus > /var/backups/zeus-pre-migrate-$(date +%F).dump    # trước MỌI lần migrate
/opt/zeus/venv/bin/python -m zeus.storage.migrate --status
/opt/zeus/venv/bin/python -m zeus.storage.migrate          # 000 shared, 101 A, 201-202 B, 301 C, 401 D
```
Migration append-only có checksum: sửa file đã áp dụng => runner từ chối (không tự sửa DB).

## 8. Chạy dịch vụ

```bash
sudo install -m 0644 zeus/app/deploy/zeus-api.service zeus/app/deploy/zeus-control-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zeus-control-worker zeus-api
journalctl -u zeus-api -u zeus-control-worker -f        # log JSON (trace_id, task_id, tenant_id)
```
Sửa `--host` trong `zeus-api.service` theo IP `br-core` thực tế; không bind 0.0.0.0. `/hooks/*` chỉ đi qua `zeus-edge`
(reverse proxy TLS); Workbench `/wb` chỉ trong mạng nội bộ/VPN, bật `ZEUS_WB_SECURE_COOKIES=1` khi có HTTPS.

## 9. Thin worker (mỗi VM agent)

Trên `zeus-core` phát hành token (in ra MỘT lần, DB chỉ giữ SHA-256):
```bash
/opt/zeus/venv/bin/python -m zeus.app.admin issue-worker-token w-code-1 --tenant zeusvn
```
Trên VM worker (không cài LLM, không giữ key provider/ERP):
```bash
sudo apt-get install -y python3 python3-venv git
sudo useradd --system --home /var/lib/zeus-worker --shell /usr/sbin/nologin zeus-worker
sudo python3 -m venv /opt/zeus/venv && sudo /opt/zeus/venv/bin/pip install <wheel zeusvn_brain>   # unit mẫu dùng /opt/zeus/venv
sudo install -d -o zeus-worker -g zeus-worker -m 0750 /var/lib/zeus-worker
sudo install -d -m 0750 /etc/zeus-worker
sudo install -m 0640 <gói>/zeus_worker/deploy/worker.sample.toml /etc/zeus-worker/worker.toml   # sửa worker_id, url, roots
printf '%s' '<token>' | sudo tee /etc/zeus-worker/token >/dev/null && sudo chown zeus-worker /etc/zeus-worker/token && sudo chmod 0600 /etc/zeus-worker/token
sudo install -m 0644 <gói>/zeus_worker/deploy/zeus-worker.service /etc/systemd/system/ && sudo systemctl enable --now zeus-worker
```
Bước "Chạy kiểm thử" của playbook là action `test.run`: worker chỉ nhận khi `executors.test_repo` (nằm trong
`repo_roots`) được cấu hình. Thu hồi: `python -m zeus.app.admin revoke-worker-tokens w-code-1`.

## 10. Kiểm tra sau cài

```bash
API=http://10.90.30.10:8080; AUTH="Authorization: Bearer $ZEUS_API_TOKEN"
curl -s $API/healthz                                  # status ok, claude_cloud_available false, system true
curl -s $API/readyz                                   # {"ready":true} (DB trả lời)
curl -s -o /dev/null -w '%{http_code}\n' $API/api/v1/tasks          # 401 (thiếu token)
curl -s -H "$AUTH" $API/internal/system               # actions [noop.echo,test.run], workers đã đăng ký
curl -s -H "$AUTH" -H 'X-Zeus-Tenant: zeusvn' -H 'content-type: application/json' \
  -d '{"event":{"tenant_id":"zeusvn","channel":"internal","kind":"command","text":"Chạy kiểm thử backend API","external_id":"smoke-1"}}' \
  $API/api/v1/events                                  # -> task_id
curl -s -H "$AUTH" -H 'X-Zeus-Tenant: zeusvn' $API/api/v1/tasks/<task_id>            # SUCCEEDED khi có worker test.run
curl -s -H "$AUTH" -H 'X-Zeus-Tenant: zeusvn' $API/api/v1/tasks/<task_id>/evidence   # VERIFIED_SUCCESS + test_result
```
Workbench: mở `http://<IP nội bộ>:8080/wb`, đăng nhập `operator` (đổi bằng `ZEUS_WB_USER`).
Trên máy dev có mã nguồn: `scripts/test.sh` và `scripts/cloud_exit_check.sh` (7 hạng mục PASS).

## 11. Sao lưu

- DB: `pg_dump -Fc zeus` hằng ngày + trước mỗi migrate; thử restore định kỳ sang DB tạm.
- Artifact: `/var/lib/zeus/artifacts` (content-addressed, kiểm bằng sha256 — `ArtifactStore.verify`).
- Temporal (giai đoạn 1): `/var/lib/zeus/temporal/temporal.db` (dừng `zeus-temporal` trước khi chép).
- Project Brain: `zeus.brain.backup.BrainBackup` (JSONL + sha256 + restore kiểm `data_match`).

## 12. Rollback

1. `sudo systemctl stop zeus-api zeus-control-worker` (worker VM tự chờ, lease hết hạn sẽ được giao lại).
2. Mã: quay `/opt/zeus/app` về tag/commit trước (`git checkout <tag>`), cài lại `pip install /opt/zeus/app`.
3. DB: migration không có "down". Nếu bản mới đã migrate: `pg_restore --clean -d zeus /var/backups/zeus-pre-migrate-<ngày>.dump`
   (mất dữ liệu ghi sau thời điểm dump — ghi nhận vào DECISION_LEDGER).
4. Workflow đang chạy: thay đổi cấu trúc TaskWorkflow cần `workflow.patched`; nếu rollback mã workflow, chờ workflow cũ
   kết thúc hoặc huỷ qua `POST /api/v1/tasks/<id>/cancel` trước.
5. `sudo systemctl start zeus-control-worker zeus-api`, chạy lại mục 10.
Gỡ hẳn: `systemctl disable --now zeus-api zeus-control-worker zeus-temporal`, `DROP DATABASE zeus`, xoá `/var/lib/zeus`,
`/opt/zeus`, `/etc/zeus` — không đụng gói/cluster Postgres dùng chung.

## 13. Sự cố thường gặp

| Triệu chứng | Nguyên nhân / xử lý |
|---|---|
| `/api/v1/*` trả 503 "chưa cấu hình ZEUS_API_TOKEN" | env prod thiếu token (fail closed) — đặt `ZEUS_API_TOKEN`. |
| Task đứng ở RUNNING, assignment `QUEUED` không worker | Không worker nào có capability `action:test.run` + `python` hoặc heartbeat quá hạn; xem `/wb/workers`, `schedule_decisions.candidates[].rejected_reason`. |
| Task AWAITING_APPROVAL | Bước R2/R3 cần người duyệt ở `/wb/approvals` hoặc `POST /api/v1/approvals/<id>/decision`. Hết hạn => EXPIRED. |
| Verdict NEEDS_HUMAN, outcome UNVERIFIED | Không có bằng chứng mạnh (bước chưa có typed action); đúng thiết kế "claims are not evidence". |
| Webhook kênh 404 | Thiếu secret kênh trong env => kênh tắt. 401 => chữ ký sai (không lộ lý do). |
