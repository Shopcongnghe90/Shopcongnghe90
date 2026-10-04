# shellcheck shell=bash
# Chọn interpreter: $ZEUS_PYTHON > .venv > /opt/zeus/venv. Source file này, dùng biến $PY.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${ZEUS_PYTHON:-}" ]]; then
  PY="$ZEUS_PYTHON"
elif [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PY="$REPO_ROOT/.venv/bin/python"
elif [[ -x /opt/zeus/venv/bin/python ]]; then
  PY=/opt/zeus/venv/bin/python
else
  echo "Không tìm thấy Python env (.venv hoặc /opt/zeus/venv). Chạy scripts/dev_bootstrap.sh trước." >&2
  exit 2
fi
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
