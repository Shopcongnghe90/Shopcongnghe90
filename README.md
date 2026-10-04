# Shopcongnghe90 — ZeusVN Brain

ZEUS Intelligence OS: hệ điều hành điều phối–kiểm soát–học tập trung tâm của shop ZEUS VN.

- Kiến trúc (v1.0, khoá): `docs/architecture/ZEUSVN_BRAIN_MASTER_ARCHITECTURE.md`
- Trạng thái + bước kế tiếp: `docs/state/MASTER_STATE.md` · Quyết định: `docs/state/DECISION_LEDGER.md`
- Workstreams: `docs/workstreams/`
- Thiết kế máy ảo cũ (đang reconcile sang thin worker): `infra/may-ao/`

```bash
scripts/dev_bootstrap.sh      # tạo .venv (không đụng host)
scripts/test.sh               # toàn bộ test (PostgreSQL 16 + Temporal dev server cục bộ)
scripts/cloud_exit_check.sh   # chứng minh không phụ thuộc Claude Cloud
```
