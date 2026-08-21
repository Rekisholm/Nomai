#!/usr/bin/env bash
# analysis: offline cell-level backtrace based on transaction snapshot parsing.
# Supports basic / priority / llm modes, plus all to run every mode in sequence.
# Requires stop to have persisted request_db_read.log and request_db_write.log in the experiment directory.
#
# Usage:
#   ./analysis.sh <experiment> <scene> [--mode <basic|priority|llm|all>] [root_rid]
#   ./analysis.sh <experiment> <scene>                     # Default: --mode all
#   ./analysis.sh <experiment> <scene> --mode basic        # Run only basic
#   ./analysis.sh <experiment> <scene> --mode llm root_rid # Specify root_rid
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/scene_lib.sh"

usage() {
  cat <<EOF
Usage: $0 <experiment> <scene> [--mode <basic|priority|llm|all>] [root_rid]

Modes:
  basic       Full transaction-snapshot backtrace (default)
  priority    rho+kappa ranked-task backtrace without a budget cap
  llm         Full backtrace plus DeepSeek cell-level pruning
  all         Run the three modes above in sequence (default)
EOF
}

# --- Parse Arguments ---
EXPERIMENT=""
SCENE_ARG=""
ROOT_RID=""
MODE="all"

while [ $# -gt 0 ]; do
  case "$1" in
    --mode)
      MODE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      if [ -z "$EXPERIMENT" ]; then
        EXPERIMENT="$1"
      elif [ -z "$SCENE_ARG" ]; then
        SCENE_ARG="$1"
      else
        ROOT_RID="$1"
      fi
      shift
      ;;
  esac
done

[ -n "$EXPERIMENT" ] && [ -n "$SCENE_ARG" ] || { usage; exit 1; }

case "$MODE" in
  basic|priority|llm|all) ;;
  *) die "invalid mode: $MODE (expected: basic|priority|llm|all)" ;;
esac

# --- Resolve Scene Paths ---
require_scene "$SCENE_ARG"
RUN_DIR="$(run_dir_for "$EXPERIMENT" "$SCENE_NAME")"
ATTACK_JSON="${RUN_DIR}/attack_rids.json"
READ_LOG="${RUN_DIR}/request_db_read.log"
WRITE_LOG="${RUN_DIR}/request_db_write.log"
REQUEST_CONTEXT_LOG="${RUN_DIR}/request_context.log"
PRIORITY_CONTEXT_ARGS=""
if { [ "$SCENE_NAME" = "superset" ] || [ "$SCENE_NAME" = "admidio" ] || [ "$SCENE_NAME" = "airflow" ] || [ "$SCENE_NAME" = "pgadmin" ] || [ "$SCENE_NAME" = "kanboard" ] || [ "$SCENE_NAME" = "moodle" ] || [ "$SCENE_NAME" = "gitlab8.13" ] || [ "$SCENE_NAME" = "gitlab14.1" ] || [ "$SCENE_NAME" = "dolphinscheduler" ] || [ "$SCENE_NAME" = "ofbiz" ] || [ "$SCENE_NAME" = "geoserver" ] || [ "$SCENE_NAME" = "flowable" ]; } && [ -s "$REQUEST_CONTEXT_LOG" ]; then
  PRIORITY_CONTEXT_ARGS="--request-context-log ${REQUEST_CONTEXT_LOG}"
fi

[ -f "$ATTACK_JSON" ] || die "attack_rids.json not found (run attack first): $ATTACK_JSON"
[ -f "$READ_LOG" ] || die "request_db_read.log not found (run stop first): $READ_LOG"
[ -f "$WRITE_LOG" ] || die "request_db_write.log not found (run stop first): $WRITE_LOG"

# --- Resolve root_rid ---
if [ -z "$ROOT_RID" ]; then
  ROOT_RID="$("$PYTHON_BIN" - "$ATTACK_JSON" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data.get("root_rid") or "")
PY
)"
fi
[ -n "$ROOT_RID" ] || die "root_rid is empty (attack extraction found no X-Request-Id)"

echo "scene=$SCENE_NAME  mode=$MODE  root_rid=$ROOT_RID"
echo "run_dir=$RUN_DIR"
echo

# --- Check LLM env-file ---
ENV_FILE=""
if [ "$MODE" == "llm" ] || [ "$MODE" == "all" ]; then
  if [ -f "${ROOT_DIR}/analysis_llm/.env" ]; then
    ENV_FILE="${ROOT_DIR}/analysis_llm/.env"
  else
    echo "[WARN] analysis_llm/.env not found, llm mode will be skipped"
  fi
fi

# --- Helper: Run and Evaluate One Mode ---
run_mode() {
  local mode_name="$1"    # basic / priority / llm
  local label="$2"        # Display label
  local extra_args="$3"   # Extra args, such as --policy xxx or --env-file xxx

  local json_out="${RUN_DIR}/cell_backtrace_${label}.json"
  local dot_out="${RUN_DIR}/causal_tree_${label}.html"
  local metrics_out="${RUN_DIR}/metrics_${label}.json"

  echo "--- [${label}] mode=${mode_name} ---"

  if [ "$mode_name" == "llm" ]; then
    if [ -z "$ENV_FILE" ]; then
      echo "  SKIP: no env-file for llm mode"
      echo '{"precision":0,"recall":0,"f1":0,"skip_reason":"no .env"}' > "$metrics_out"
      return 0
    fi
  fi

  "$PYTHON_BIN" -m analysis_cell \
    --mode "$mode_name" \
    $extra_args \
    --read-log "$READ_LOG" \
    --write-log "$WRITE_LOG" \
    --root-rid "$ROOT_RID" \
    --output-dot "$dot_out" \
    --output-json "$json_out" 2>&1 | tail -5

  "$PYTHON_BIN" "${SCRIPT_DIR}/lib/eval_cell.py" \
    "$json_out" "$ATTACK_JSON" "$metrics_out" 2>&1 | tail -1

  echo
}

# --- Execute by Mode ---
case "$MODE" in
  basic)
    run_mode basic basic ""
    ;;
  priority)
    run_mode priority priority "$PRIORITY_CONTEXT_ARGS"
    ;;
  llm)
    run_mode llm llm "--env-file ${ENV_FILE}"
    ;;
  all)
    echo "=== Running all 3 modes ==="
    echo

    # 1. basic
    run_mode basic basic ""

    # 2. priority
    run_mode priority priority "$PRIORITY_CONTEXT_ARGS"

    # 3. llm
    if [ -n "$ENV_FILE" ]; then
      run_mode llm llm "--env-file ${ENV_FILE}"
    else
      echo "--- [llm] SKIP: no .env ---"
      echo '{"precision":0,"recall":0,"f1":0,"skip_reason":"no .env"}' > "${RUN_DIR}/metrics_llm.json"
      echo
    fi
    ;;
esac

echo "=== analysis done: $RUN_DIR ==="
