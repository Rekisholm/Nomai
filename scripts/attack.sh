#!/usr/bin/env bash
# attack: run the scene attack script, extract the X-Request-Id attack chain, and persist attack_rids.json.
# Usage: ./attack.sh <experiment> <scene>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/scene_lib.sh"

usage() {
  echo "Usage: $0 <experiment> <scene>"
}

[ $# -eq 2 ] || { usage; exit 1; }

EXPERIMENT="$1"
SCENE_ARG="$2"

require_scene "$SCENE_ARG"
RUN_DIR="$(run_dir_for "$EXPERIMENT" "$SCENE_NAME")"
ATTACK_SCRIPT="$(attack_script_for_scene)"

[ -f "$ATTACK_SCRIPT" ] || die "attack script not found: $ATTACK_SCRIPT"
mkdir -p "$RUN_DIR"

echo "attack_script=$ATTACK_SCRIPT" > "${RUN_DIR}/attack_info.txt"
echo "started_at=$(date -Is)" >> "${RUN_DIR}/attack_info.txt"

echo "[1/2] run attack: $ATTACK_SCRIPT"
set +e
(
  cd "$SCENE_DIR"
  "$PYTHON_BIN" "$ATTACK_SCRIPT"
) > "${RUN_DIR}/attack_raw.log" 2>&1
status=$?
set -e

echo "[2/2] extract X-Request-Id chain"
"$PYTHON_BIN" "${SCRIPT_DIR}/lib/extract_attack_rids.py" \
  "${RUN_DIR}/attack_raw.log" \
  "${RUN_DIR}/attack_rids.json" \
  "$SCENE_NAME" | tee "${RUN_DIR}/attack_rids.out"

echo "exit_status=$status" >> "${RUN_DIR}/attack_info.txt"
echo "stopped_at=$(date -Is)" >> "${RUN_DIR}/attack_info.txt"
exit "$status"
