#!/usr/bin/env python3
"""Run attack_backtrace with read/write logs and the final attack RID from an experiment directory."""
from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from analysis import attack_backtrace as bt  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: run_backtrace.py <run_dir> <root_rid>", file=sys.stderr)
        return 1

    run_dir = Path(sys.argv[1]).resolve()
    root_rid = sys.argv[2]
    read_log = run_dir / "request_db_read.log"
    write_log = run_dir / "request_db_write.log"
    dot_path = run_dir / "causal_tree_with_data.dot"

    if not read_log.exists():
        raise FileNotFoundError(read_log)
    if not write_log.exists():
        raise FileNotFoundError(write_log)

    reads = bt.load_read_logs(str(read_log))
    writes = bt.load_write_logs(str(write_log))
    indexes = bt.build_indexes(reads, writes)
    graph = bt.build_causal_graph_with_data(root_rid, *indexes)
    bt.export_graphviz_with_data(root_rid, graph[0], graph[5], str(dot_path))

    summary = {
        "root_rid": root_rid,
        "read_log": str(read_log),
        "write_log": str(write_log),
        "dot": str(dot_path),
        "read_events": len(reads),
        "write_events": len(writes),
    }
    (run_dir / "backtrace_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"dot={dot_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
