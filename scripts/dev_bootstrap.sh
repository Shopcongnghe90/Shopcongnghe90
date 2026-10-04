#!/usr/bin/env bash
# Dựng môi trường dev/test trên Ubuntu (KHÔNG đụng host production: chỉ tạo .venv trong repo).
# Postgres/Temporal là hướng dẫn — script không tự cài gói hệ thống (tránh thay đổi host ERP).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
PYBIN="${PYBIN:-python3}"

"$PYBIN" - <<'PY'
import sys
assert sys.version_info >= (3, 11), f"cần Python >= 3.11, đang có {sys.version}"
PY

if [[ ! -x .venv/bin/python ]]; then
  "$PYBIN" -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -e ".[test]"
echo "OK: .venv sẵn sàng ($(.venv/bin/python --version))"

cat <<'TXT'

== Phụ thuộc ngoài Python (cài thủ công, có người duyệt nếu là host production) ==

1) PostgreSQL 16 + pgvector (Ubuntu 24.04):
     sudo apt-get install -y postgresql-16 postgresql-16-pgvector
   Test chỉ cần binary tại /usr/lib/postgresql/16/bin (đặt ZEUS_PG_BIN nếu khác);
   fixture tự initdb cluster tạm, không đụng cluster đang chạy.

2) Temporal CLI (dev server cho test + môi trường dev):
     Tải bản phát hành từ GitHub: https://github.com/temporalio/cli/releases
     (vd temporal_cli_<version>_linux_amd64.tar.gz), giải nén, đặt binary tại /opt/zeus/bin/temporal
     hoặc đặt ZEUS_TEMPORAL_CLI=/đường/dẫn/temporal. Kiểm tra: temporal --version
   Production: Temporal server + Postgres persistence trên VM zeus-core (xem docs/architecture).

3) Kiểm tra: scripts/test.sh && scripts/cloud_exit_check.sh
TXT
