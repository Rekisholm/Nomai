#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/scene_lib.sh"

usage() {
  cat <<'EOF'
Usage:
  ./run_scene.sh list
  ./run_scene.sh start    <experiment> <scene> [--build] [--cell-cdc] [--no-cdc] [--no-health]
  ./run_scene.sh normal   <experiment> <scene> [locust_seconds]
  ./run_scene.sh attack   <experiment> <scene>
  ./run_scene.sh stop     <experiment> <scene> [--keep-locust]
  ./run_scene.sh analysis <experiment> <scene> [--mode <basic|priority|llm|all>] [root_rid]
  ./run_scene.sh budget_sweep <experiment> <scene> [root_rid]
  ./run_scene.sh status   <experiment> <scene>

Legacy (row-level, deprecated):
  ./run_scene.sh analysis_prune <experiment> <scene> [root_rid]

Legacy:
  ./run_scene.sh <scene>          # Quick-start one scene with an auto-generated experiment id.
EOF
}

cmd="${1:-}"
[ -n "$cmd" ] || { usage; exit 1; }

case "$cmd" in
  list)
    list_scenes
    ;;
  start)
    shift
    exec "${SCRIPT_DIR}/start.sh" "$@"
    ;;
  normal)
    shift
    exec "${SCRIPT_DIR}/normal.sh" "$@"
    ;;
  attack)
    shift
    exec "${SCRIPT_DIR}/attack.sh" "$@"
    ;;
  stop)
    shift
    exec "${SCRIPT_DIR}/stop.sh" "$@"
    ;;
  analysis)
    shift
    exec "${SCRIPT_DIR}/analysis.sh" "$@"
    ;;
  budget_sweep)
    shift
    exec "${SCRIPT_DIR}/budget_sweep_cmd.sh" "$@"
    ;;
  analysis_prune)
    shift
    exec "${SCRIPT_DIR}/analysis_prune.sh" "$@"
    ;;
  status)
    [ $# -eq 3 ] || { usage; exit 1; }
    EXPERIMENT="$2"
    require_scene "$3"
    RUN_DIR="$(run_dir_for "$EXPERIMENT" "$SCENE_NAME")"
    echo "scene=$SCENE_NAME"
    echo "run_dir=$RUN_DIR"
    echo
    compose ps || true
    echo
    if [ -f "${RUN_DIR}/cdc.pid" ]; then
      pid="$(cat "${RUN_DIR}/cdc.pid" 2>/dev/null || true)"
      cdc_mode="unknown"
      if [ -f "${RUN_DIR}/run_info.txt" ]; then
        cdc_mode="$(sed -n 's/^cdc_mode=//p' "${RUN_DIR}/run_info.txt" | tail -1)"
      fi
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "cdc=running mode=${cdc_mode:-unknown} pid=$pid"
      else
        echo "cdc=stopped mode=${cdc_mode:-unknown}"
      fi
    else
      echo "cdc=no pidfile"
    fi
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    if [ $# -eq 1 ]; then
      experiment="manual_$(date +%Y%m%d_%H%M%S)"
      exec "${SCRIPT_DIR}/start.sh" "$experiment" "$cmd"
    fi
    usage
    exit 1
    ;;
esac
