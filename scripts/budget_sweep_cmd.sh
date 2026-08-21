#!/usr/bin/env bash
# budget_sweep: run budget sweep analysis for one scene, comparing priority/FIFO/random.
# Write budget_sweep.json and budget_sweep_report.md to the experiment directory.
#
# Usage: ./budget_sweep_cmd.sh <experiment> <scene> [root_rid]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/scene_lib.sh"

[ $# -ge 2 ] || { echo "Usage: $0 <experiment> <scene> [root_rid]"; exit 1; }

EXPERIMENT="$1"
SCENE_ARG="$2"
ROOT_RID="${3:-}"

require_scene "$SCENE_ARG"
RUN_DIR="$(run_dir_for "$EXPERIMENT" "$SCENE_NAME")"
ATTACK_JSON="${RUN_DIR}/attack_rids.json"

[ -f "$ATTACK_JSON" ] || die "attack_rids.json not found: $ATTACK_JSON"

if [ -z "$ROOT_RID" ]; then
  ROOT_RID="$("$PYTHON_BIN" - "$ATTACK_JSON" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("root_rid") or "")
PY
)"
fi
[ -n "$ROOT_RID" ] || die "root_rid is empty"

echo "scene=$SCENE_NAME  root_rid=$ROOT_RID"
echo "run_dir=$RUN_DIR"

# Create a temporary directory containing only this scene.
TMP_DIR="$(mktemp -d)"
SCENE_LINK="${TMP_DIR}/${SCENE_NAME}"
ln -s "$RUN_DIR" "$SCENE_LINK"

PYTHONPATH="${ROOT_DIR}" "$PYTHON_BIN" "${SCRIPT_DIR}/lib/budget_sweep.py" \
  --run-dir "$TMP_DIR" \
  --output "${RUN_DIR}/budget_sweep.json" 2>&1

rm -rf "$TMP_DIR"

# Extract this scene's summary from JSON.
"$PYTHON_BIN" - "${RUN_DIR}/budget_sweep.json" "${SCENE_NAME}" <<'PY'
import json, sys

sweep = json.load(open(sys.argv[1]))
scene_name = sys.argv[2]

for s in sweep["scenes"]:
    if s["scene"] != scene_name:
        continue
    p = s["priority"]
    p1 = s.get("priority_v1")
    f = s["fifo"]
    r = s["random"]
    print(f"\n=== {scene_name} budget sweep ===")
    print(f"  full_budget:      {p['full_budget']}")
    print(f"  B_priority:       {p['b_complete']}  (reduction {p['budget_reduction']*100:.1f}%)")
    if p1:
        print(f"  B_priority_v1:    {p1['b_complete']}  (v2 delta {p['b_complete']-p1['b_complete']:+d})")
        print(f"  context records:  {s['request_contexts_loaded']}")
    print(f"  B_FIFO:           {f['b_complete']}")
    print(f"  B_random:         {r['b_complete_mean']:.0f} +/- {r['b_complete_std']:.0f}")
    print(f"  F1-AUC  pri/fifo: {p['f1_auc']:.4f} / {f['f1_auc']:.4f}")
    print(f"  R-AUC   pri/fifo: {p['recall_auc']:.4f} / {f['recall_auc']:.4f}")
    break
PY

echo "output=${RUN_DIR}/budget_sweep.json"
