#!/usr/bin/env bash
# analysis_prune: offline pruned backtrace. Build a noise policy from Locust normal traffic, then run and evaluate pruned backtrace.
# Requires stop to have persisted request_db_read.log, request_db_write.log, and locust_request_log in the experiment directory.
# Usage: ./analysis_prune.sh <experiment> <scene> [root_rid]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/scene_lib.sh"

usage() {
  echo "Usage: $0 <experiment> <scene> [root_rid]"
}

[ $# -ge 2 ] || { usage; exit 1; }

EXPERIMENT="$1"
SCENE_ARG="$2"
ROOT_RID="${3:-}"

require_scene "$SCENE_ARG"
RUN_DIR="$(run_dir_for "$EXPERIMENT" "$SCENE_NAME")"
ATTACK_JSON="${RUN_DIR}/attack_rids.json"
READ_LOG="${RUN_DIR}/request_db_read.log"
WRITE_LOG="${RUN_DIR}/request_db_write.log"
LOCUST_LOG_DIR="${RUN_DIR}/locust_request_log"
REQUEST_LOG="${RUN_DIR}/normal_request.log"
POLICY_JSON="${RUN_DIR}/pruning_policy.json"
DOT_PATH="${RUN_DIR}/causal_tree_with_data_pruned.dot"
STATS_JSON="${RUN_DIR}/causal_tree_with_data_pruned.stats.json"
METRICS_JSON="${RUN_DIR}/metrics_pruned.json"

[ -f "$ATTACK_JSON" ] || die "attack_rids.json not found (run attack first): $ATTACK_JSON"
[ -f "$READ_LOG" ] || die "request_db_read.log not found (run stop first): $READ_LOG"
[ -f "$WRITE_LOG" ] || die "request_db_write.log not found (run stop first): $WRITE_LOG"
[ -d "$LOCUST_LOG_DIR" ] || die "locust_request_log not found (run normal + stop first): $LOCUST_LOG_DIR"

# By default, use the last attack-chain step as the backtrace root_rid.
if [ -z "$ROOT_RID" ]; then
  ROOT_RID="$("$PYTHON_BIN" - "$ATTACK_JSON" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data.get("root_rid") or "")
PY
)"
fi

[ -n "$ROOT_RID" ] || die "root_rid is empty (attack extraction found no X-Request-Id)"

echo "[1/4] merge locust request logs"
"$PYTHON_BIN" - "$LOCUST_LOG_DIR" "$REQUEST_LOG" <<'PY'
import json
import sys
from pathlib import Path

log_dir = Path(sys.argv[1])
out_path = Path(sys.argv[2])
files = sorted(log_dir.glob("*.jsonl"))
if not files:
    raise SystemExit(f"no locust request jsonl found: {log_dir}")

count = 0
with out_path.open("w", encoding="utf-8") as out:
    for path in files:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = json.loads(line)
                if not record.get("rid"):
                    continue
                out.write(json.dumps({
                    "rid": str(record["rid"]),
                    "time": record["time"],
                    "method": record["method"],
                    "path": record["path"],
                }, ensure_ascii=False, separators=(",", ":")) + "\n")
                count += 1

if count == 0:
    raise SystemExit(f"no request records with rid found: {log_dir}")
print(f"merged_request_events={count}")
PY

echo "[2/4] build pruning policy from normal traffic"
"$PYTHON_BIN" "${ROOT_DIR}/analysis/pruning_algorithm.py" \
  --request-log "$REQUEST_LOG" \
  --read-log "$READ_LOG" \
  --write-log "$WRITE_LOG" \
  --output "$POLICY_JSON"

echo "[3/4] pruned backtrace from root_rid=$ROOT_RID"
"$PYTHON_BIN" "${ROOT_DIR}/analysis/attack_backtrace_pruning.py" \
  --read-log "$READ_LOG" \
  --write-log "$WRITE_LOG" \
  --root-rid "$ROOT_RID" \
  --policy "$POLICY_JSON" \
  --output "$DOT_PATH" \
  --stats-output "$STATS_JSON"

echo "[4/4] evaluate pruned dot vs attack RIDs (Precision/Recall/F1)"
"$PYTHON_BIN" "${SCRIPT_DIR}/lib/eval_dot.py" \
  "$DOT_PATH" \
  "$ATTACK_JSON" \
  "$METRICS_JSON"

echo "run_dir=$RUN_DIR"
