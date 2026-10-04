#!/usr/bin/env bash
# Cloud Exit check: chứng minh hệ thống build/test/chạy KHÔNG cần Claude Cloud.
# - CLAUDE_CLOUD_AVAILABLE=false, gỡ mọi biến CLAUDE_CODE_* khỏi môi trường test
# - quét mã runtime (zeus/, zeus_worker/) tìm phụ thuộc cấm (scripts/cloud_exit_forbidden.txt)
# - chạy toàn bộ test, rồi từng hạng mục theo marker pytest
# Kết quả: bảng BUILD/TEST/WORKFLOW/MODEL_FALLBACK/PROJECT_BRAIN/WORKER_CONTROL/EVIDENCE = PASS/FAIL/NOT_YET.
# NOT_YET = chưa có test cho hạng mục (chưa có code). Hạng mục có test nhưng bị skip => FAIL (chưa chứng minh).
# Exit != 0 nếu có FAIL.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_python.sh"
cd "$REPO_ROOT" || exit 2

export CLAUDE_CLOUD_AVAILABLE=false
for v in $(env | grep -oE '^CLAUDE_CODE_[A-Za-z0-9_]*' || true); do unset "$v"; done

OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT
declare -A RES NOTE

# ---------------------------------------------------------------- BUILD
build_ok=1
PATTERNS="$OUT/patterns.txt"
grep -vE '^\s*(#|$)' scripts/cloud_exit_forbidden.txt > "$PATTERNS"
if grep -rniE -f "$PATTERNS" --exclude-dir=__pycache__ zeus zeus_worker > "$OUT/scan.txt" 2>/dev/null; then
  build_ok=0; NOTE[BUILD]="phụ thuộc cấm trong mã runtime (xem bên dưới)"
fi
"$PY" -m compileall -q zeus zeus_worker > "$OUT/compile.txt" 2>&1 || { build_ok=0; NOTE[BUILD]="compileall lỗi"; }
"$PY" - > "$OUT/import.txt" 2>&1 <<'PY' || { build_ok=0; NOTE[BUILD]="import lỗi"; }
import importlib, pkgutil
import zeus, zeus_worker
for pkg in (zeus, zeus_worker):
    for m in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
        importlib.import_module(m.name)
PY
if "$PY" -m pip --version >/dev/null 2>&1; then
  "$PY" -m pip wheel --no-deps --no-build-isolation -q -w "$OUT/wheel" . > "$OUT/wheel.txt" 2>&1 \
    || { build_ok=0; NOTE[BUILD]="build wheel lỗi"; }
  rm -rf build ./*.egg-info
fi
RES[BUILD]=$([[ $build_ok == 1 ]] && echo PASS || echo FAIL)

# ---------------------------------------------------------------- TEST (toàn bộ)
"$PY" -m pytest -q -p no:cacheprovider > "$OUT/all.txt" 2>&1
rc=$?
RES[TEST]=$([[ $rc == 0 ]] && echo PASS || echo FAIL)
NOTE[TEST]="$(grep -E '^[=]*.*(passed|failed|error|no tests ran)' "$OUT/all.txt" | tail -1 | tr -d '=' | sed 's/^ *//')"

# ---------------------------------------------------------------- hạng mục theo marker
category() {  # $1=tên hạng mục  $2=marker
  local name="$1" marker="$2" log="$OUT/$2.txt" rc summary passed failed skipped
  "$PY" -m pytest -q -p no:cacheprovider -m "$marker" > "$log" 2>&1
  rc=$?
  summary="$(grep -E '(passed|failed|error|skipped|deselected|no tests ran)' "$log" | tail -1 | tr -d '=' | sed 's/^ *//')"
  passed=$(grep -oE '[0-9]+ passed' <<<"$summary" | grep -oE '[0-9]+' || echo 0)
  failed=$(grep -oE '[0-9]+ (failed|errors?)' <<<"$summary" | grep -oE '[0-9]+' | awk '{s+=$1} END {print s+0}')
  skipped=$(grep -oE '[0-9]+ skipped' <<<"$summary" | grep -oE '[0-9]+' || echo 0)
  if [[ $rc == 5 ]]; then
    RES[$name]=NOT_YET; NOTE[$name]="chưa có test marker $marker"
  elif [[ $rc != 0 || ${failed:-0} != 0 ]]; then
    RES[$name]=FAIL; NOTE[$name]="$summary"
  elif [[ ${passed:-0} == 0 ]]; then
    RES[$name]=FAIL; NOTE[$name]="có test nhưng bị skip ($skipped) — chưa chứng minh"
  else
    RES[$name]=PASS; NOTE[$name]="$summary"
  fi
}
category WORKFLOW cloud_exit_workflow
category MODEL_FALLBACK cloud_exit_model_fallback
category PROJECT_BRAIN cloud_exit_project_brain
category WORKER_CONTROL cloud_exit_worker_control
category EVIDENCE cloud_exit_evidence

# ---------------------------------------------------------------- báo cáo
echo "== ZeusVN Brain — Cloud Exit check (CLAUDE_CLOUD_AVAILABLE=$CLAUDE_CLOUD_AVAILABLE) =="
printf '%-16s %-8s %s\n' "CATEGORY" "RESULT" "NOTE"
fail=0
for k in BUILD TEST WORKFLOW MODEL_FALLBACK PROJECT_BRAIN WORKER_CONTROL EVIDENCE; do
  printf '%-16s %-8s %s\n' "$k" "${RES[$k]}" "${NOTE[$k]:-}"
  [[ ${RES[$k]} == FAIL ]] && fail=1
done
if [[ -s "$OUT/scan.txt" ]]; then echo; echo "-- Phụ thuộc cấm:"; cat "$OUT/scan.txt"; fi
if [[ ${RES[TEST]} == FAIL ]]; then echo; echo "-- pytest (đuôi):"; tail -40 "$OUT/all.txt"; fi
for f in compile import wheel; do
  if [[ ${RES[BUILD]} == FAIL && -s "$OUT/$f.txt" ]]; then echo "-- $f:"; tail -20 "$OUT/$f.txt"; fi
done
echo
if [[ $fail == 1 ]]; then echo "CLOUD_EXIT: FAIL"; exit 1; fi
echo "CLOUD_EXIT: OK (không có FAIL; NOT_YET = hạng mục chưa có code/test)"
