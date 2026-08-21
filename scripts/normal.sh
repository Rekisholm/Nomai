#!/usr/bin/env bash
# normal: start Locust background traffic in the background and store its pid in the experiment directory.
# Usage: ./normal.sh <experiment> <scene> [run_time_seconds]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/scene_lib.sh"

usage() {
  echo "Usage: $0 <experiment> <scene> [run_time_seconds]"
}

[ $# -ge 2 ] || { usage; exit 1; }

EXPERIMENT="$1"
SCENE_ARG="$2"
RUN_SECONDS="${3:-120}"

require_scene "$SCENE_ARG"
RUN_DIR="$(run_dir_for "$EXPERIMENT" "$SCENE_NAME")"
LOCUST_DIR="$(locust_dir_for_scene)" || die "no locustfile for scene: $SCENE_NAME"
LOCUST_FILE="${LOCUST_DIR}/locustfile.py"

[ -f "$LOCUST_FILE" ] || die "locustfile not found: $LOCUST_FILE"

mkdir -p "$RUN_DIR"
# Locust request_log may be written by the container as root, so cleanup must tolerate failures.
if [ -d "${LOCUST_DIR}/request_log" ]; then
  rm -rf "${LOCUST_DIR}/request_log" 2>/dev/null || \
    docker run --rm -v "${LOCUST_DIR}:/t" pg-request-logger:13.12 \
      sh -c 'rm -rf /t/request_log' >/dev/null 2>&1 || true
fi

{
  echo "locust_dir=$LOCUST_DIR"
  echo "run_seconds=$RUN_SECONDS"
  echo "locust_users=${LOCUST_USERS:-5}"
  echo "locust_spawn_rate=${LOCUST_SPAWN_RATE:-5}"
  echo "locust_csv_prefix=${RUN_DIR}/locust_stats"
  echo "started_at=$(date -Is)"
} > "${RUN_DIR}/locust_info.txt"

echo "[1/1] start locust (background) ${RUN_SECONDS}s: $LOCUST_DIR"
(
  cd "$LOCUST_DIR"
  exec "$PYTHON_BIN" -m locust -f ./locustfile.py \
    --headless -u "${LOCUST_USERS:-5}" -r "${LOCUST_SPAWN_RATE:-5}" \
    --run-time "${RUN_SECONDS}s" \
    --csv "${RUN_DIR}/locust_stats" --csv-full-history
) > "${RUN_DIR}/locust.out" 2>&1 &

LOCUST_PID="$!"
echo "$LOCUST_PID" > "${RUN_DIR}/locust.pid"
echo "locust_pid=$LOCUST_PID" >> "${RUN_DIR}/locust_info.txt"
echo "locust_pid=$LOCUST_PID"
echo "run_dir=$RUN_DIR"
