#!/usr/bin/env bash
# start: launch a scene with docker compose, run the health check, and start the CDC listener.
# Usage: ./start.sh <experiment> <scene> [--build] [--cell-cdc] [--no-cdc] [--no-health]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/scene_lib.sh"

usage() {
  echo "Usage: $0 <experiment> <scene> [--build] [--cell-cdc] [--no-cdc] [--no-health]"
}

[ $# -ge 2 ] || { usage; exit 1; }

EXPERIMENT="$1"
SCENE_ARG="$2"
shift 2

BUILD_FLAG=""
NO_CDC=0
NO_HEALTH=0
CELL_CDC=0

while [ $# -gt 0 ]; do
  case "$1" in
    --build) BUILD_FLAG="--build" ;;
    --cell-cdc) CELL_CDC=1 ;;
    --no-cdc) NO_CDC=1 ;;
    --no-health) NO_HEALTH=1 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

[ "$NO_CDC" -eq 0 ] || [ "$CELL_CDC" -eq 0 ] || \
  die "--cell-cdc cannot be used with --no-cdc"

require_scene "$SCENE_ARG"

RUN_DIR="$(run_dir_for "$EXPERIMENT" "$SCENE_NAME")"
mkdir -p "$RUN_DIR"

CDC_MODE="row"
CDC_LISTENER="listener.py"
if [ "$NO_CDC" -eq 1 ]; then
  CDC_MODE="disabled"
elif [ "$CELL_CDC" -eq 1 ]; then
  CDC_MODE="cell"
  CDC_LISTENER="listener_cell.py"
fi

{
  echo "experiment=$EXPERIMENT"
  echo "scene=$SCENE_NAME"
  echo "scene_dir=$SCENE_DIR"
  echo "cdc_config=$CDC_CONFIG"
  echo "cdc_mode=$CDC_MODE"
  echo "python_bin=$PYTHON_BIN"
  echo "health_url=$HEALTH_URL"
  echo "started_at=$(date -Is)"
} > "${RUN_DIR}/run_info.txt"

echo "[1/3] start docker scene: $SCENE_NAME"
prepare_scene_read_log_dirs
clear_scene_read_log
clear_scene_request_context_log
compose down -v --remove-orphans >/dev/null 2>&1 || true
if [ -n "$BUILD_FLAG" ]; then
  compose up -d "$BUILD_FLAG"
else
  compose up -d
fi

if [ "$NO_HEALTH" -eq 0 ]; then
  echo "[2/3] wait health check"
  HEALTH_CHECK_URL="$(health_url_for_run)"
  echo "$HEALTH_CHECK_URL" > "${RUN_DIR}/health_url.txt"
  wait_for_health "$HEALTH_CHECK_URL" "$HEALTH_TIMEOUT"
else
  echo "[2/3] skip health check"
fi

if [ "$NO_CDC" -eq 0 ]; then
  echo "[3/3] start ${CDC_MODE}-level CDC listener: config_${CDC_CONFIG}.json"
  kill_pidfile "${RUN_DIR}/cdc.pid"
  pkill -f "${ROOT_DIR}/cdc_listener/listener.py" 2>/dev/null || true
  pkill -f "${ROOT_DIR}/cdc_listener/listener_cell.py" 2>/dev/null || true
  : > "${RUN_DIR}/request_db_write.log"
  CVE_NAME="$CDC_CONFIG" \
  CDC_LOG_FILE="${RUN_DIR}/request_db_write.log" \
  nohup "$PYTHON_BIN" -u "${ROOT_DIR}/cdc_listener/${CDC_LISTENER}" \
    > "${RUN_DIR}/cdc_listener.out" 2>&1 &
  CDC_PID="$!"
  echo "$CDC_PID" > "${RUN_DIR}/cdc.pid"
  sleep 2
  if ! kill -0 "$CDC_PID" 2>/dev/null; then
    echo "CDC listener exited early, see: ${RUN_DIR}/cdc_listener.out" >&2
    exit 1
  fi
  echo "cdc_pid=$CDC_PID" >> "${RUN_DIR}/run_info.txt"
else
  echo "[3/3] skip CDC listener"
fi

echo "scene ready"
echo "run_dir=$RUN_DIR"
