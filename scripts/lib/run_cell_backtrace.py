#!/usr/bin/env python3
"""Run analysis_cell with read/write logs and the final attack RID from an experiment directory.

This replaces the old run_backtrace.py time-based last-writer approach with
transaction ID snapshot parsing.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from analysis_cell.main import main as cell_main  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: run_cell_backtrace.py <run_dir> <root_rid>", file=sys.stderr)
        return 1

    run_dir = Path(sys.argv[1]).resolve()
    root_rid = sys.argv[2]
    read_log = run_dir / "request_db_read.log"
    write_log = run_dir / "request_db_write.log"
    dot_path = run_dir / "causal_tree_with_cells.html"
    json_path = run_dir / "cell_backtrace.json"

    if not read_log.exists():
        raise FileNotFoundError(read_log)
    if not write_log.exists():
        raise FileNotFoundError(write_log)

    argv = [
        "--read-log", str(read_log),
        "--write-log", str(write_log),
        "--root-rid", root_rid,
        "--mode", "basic",
        "--output-dot", str(dot_path),
        "--output-json", str(json_path),
    ]
    return cell_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
