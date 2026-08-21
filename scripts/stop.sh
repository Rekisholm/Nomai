#!/usr/bin/env bash
# stop: wait for collected data to flush, organize experiment data, stop the CDC consumer, run down -v, and clean .out artifacts.
# Usage: ./stop.sh <experiment> <scene> [--keep-locust] [--keep-out]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/scene_lib.sh"

usage() {
  echo "Usage: $0 <experiment> <scene> [--keep-locust] [--keep-out]"
}

[ $# -ge 2 ] || { usage; exit 1; }

EXPERIMENT="$1"
SCENE_ARG="$2"
shift 2

KEEP_LOCUST=0
KEEP_OUT=0
while [ $# -gt 0 ]; do
  case "$1" in
    --keep-locust) KEEP_LOCUST=1 ;;
    --keep-out) KEEP_OUT=1 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

require_scene "$SCENE_ARG"
RUN_DIR="$(run_dir_for "$EXPERIMENT" "$SCENE_NAME")"
mkdir -p "$RUN_DIR"
WRITE_LOG="${RUN_DIR}/request_db_write.log"

echo "[1/6] wait locust background traffic finish"
if [ "$KEEP_LOCUST" -eq 1 ]; then
  kill_pidfile "${RUN_DIR}/locust.pid"
else
  wait_pidfile_done "${RUN_DIR}/locust.pid" "${LOCUST_WAIT_MAX:-180}"
  kill_pidfile "${RUN_DIR}/locust.pid"
fi

echo "[2/6] wait CDC write log flush to disk"
wait_write_log_stable "$WRITE_LOG"

echo "[3/6] organize experiment data (read log + locust log)"
sync_scene_read_log "$RUN_DIR"
sync_scene_request_context_log "$RUN_DIR"
sync_locust_request_log "$RUN_DIR"

echo "[4/6] kill CDC listener process"
kill_pidfile "${RUN_DIR}/cdc.pid"
pkill -f "${ROOT_DIR}/cdc_listener/listener.py" 2>/dev/null || true
pkill -f "${ROOT_DIR}/cdc_listener/listener_cell.py" 2>/dev/null || true

echo "[5/6] docker compose down -v: $SCENE_NAME"
compose down -v --remove-orphans

echo "[6/6] cleanup .out artifacts"
if [ "$KEEP_OUT" -eq 1 ]; then
  echo "keep .out (--keep-out)"
else
  rm -f "${RUN_DIR}"/*.out
fi

echo "stopped_at=$(date -Is)" >> "${RUN_DIR}/run_info.txt"
echo "stopped: run_dir=$RUN_DIR"
