#!/usr/bin/env bash
# Relaunch wrapper: hard-timeout Python if it hangs, kill leaked Chrome between runs.
set -uo pipefail

RECYCLE_HOURS="${PROCESS_RECYCLE_HOURS:-3}"
# Buffer past Python's own recycle so a hung process is force-killed.
HARD_TIMEOUT_SECONDS="${PROCESS_HARD_TIMEOUT_SECONDS:-}"
if [[ -z "${HARD_TIMEOUT_SECONDS}" ]]; then
  # bash arithmetic needs integers
  RECYCLE_INT="${RECYCLE_HOURS%%.*}"
  if [[ -z "${RECYCLE_INT}" || "${RECYCLE_INT}" -lt 1 ]]; then
    RECYCLE_INT=3
  fi
  HARD_TIMEOUT_SECONDS=$(( RECYCLE_INT * 3600 + 900 ))
fi

cleanup_browsers() {
  pkill -9 -f '[c]hrome|[c]hromium|[c]hromedriver|[c]rashpad' 2>/dev/null || true
}

echo "[start.sh] PROCESS_RECYCLE_HOURS=${RECYCLE_HOURS} HARD_TIMEOUT_SECONDS=${HARD_TIMEOUT_SECONDS}"

while true; do
  cleanup_browsers
  echo "[start.sh] launching monitor.py at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  set +e
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=60 "${HARD_TIMEOUT_SECONDS}" python -u monitor.py "$@"
    code=$?
  else
    python -u monitor.py "$@"
    code=$?
  fi
  set -e
  cleanup_browsers
  echo "[start.sh] monitor exited code=${code}; relaunching in 5s"
  sleep 5
done
