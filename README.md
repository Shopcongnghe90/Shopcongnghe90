# Shopcongnghe90 — ZeusVN Brain

ZEUS Intelligence OS: hệ điều hành điều phối–kiểm soát–học tập trung tâm của shop ZEUS VN.

- Kiến trúc (v1.0, khoá): `docs/architecture/ZEUSVN_BRAIN_MASTER_ARCHITECTURE.md`
- Trạng thái + bước kế tiếp: `docs/state/MASTER_STATE.md` · Quyết định: `docs/state/DECISION_LEDGER.md`
- Workstreams: `docs/workstreams/`
- Hạ tầng thin worker (Incus, gate G1): `infra/may-ao/`
- Cài server Ubuntu không cần Claude Cloud: `docs/runbooks/SERVER_BOOTSTRAP.md`

```bash
scripts/dev_bootstrap.sh      # tạo .venv (không đụng host)
scripts/test.sh               # toàn bộ test (PostgreSQL 16 + Temporal dev server cục bộ)
scripts/cloud_exit_check.sh   # chứng minh không phụ thuộc Claude Cloud

# chạy hệ thống (cần ZEUS_DB_DSN, Temporal tại ZEUS_TEMPORAL_ADDRESS)
python -m zeus.storage.migrate
uvicorn --factory zeus.app.main:create_server_app --host 127.0.0.1 --port 8080   # /api/v1 /worker/v1 /wb /hooks
python -m zeus.app.worker_main                                                     # Temporal worker control
python -m zeus_worker --config /etc/zeus-worker/worker.toml                        # thin worker
```
