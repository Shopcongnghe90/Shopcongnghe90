#!/usr/bin/env bash
# Chạy toàn bộ test (gồm pg + temporal nếu có binary). Tham số thêm truyền thẳng cho pytest.
#   scripts/test.sh                 # tất cả
#   scripts/test.sh -m "not temporal"
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_python.sh"
cd "$REPO_ROOT"
exec "$PY" -m pytest "$@"
