from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from attack_analysis_llm.analyzers import BaseTaintAnalyzer, DeepSeekTaintAnalyzer, HeuristicTaintAnalyzer
    from attack_analysis_llm.common import load_env_file
    from attack_analysis_llm.core import build_taint_backtrace
    from attack_analysis_llm.io_utils import build_indexes, compute_paths, export_html_report, load_read_logs, load_write_logs, write_summary_json
else:
    from .analyzers import BaseTaintAnalyzer, DeepSeekTaintAnalyzer, HeuristicTaintAnalyzer
    from .common import load_env_file
    from .core import build_taint_backtrace
    from .io_utils import build_indexes, compute_paths, export_html_report, load_read_logs, load_write_logs, write_summary_json


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Attack request chain backtrace with LLM-based taint pruning")
    parser.add_argument("--read-log", required=True, help="path to the request read log")
    parser.add_argument("--write-log", required=True, help="path to the CDC write log")
    parser.add_argument("--root-rid", required=True, help="final attack request ID")
    parser.add_argument(
        "--output-html", "--output-dot",
        dest="output_html",
        default="taint_backtrace.html",
        help="path to the output HTML visualization file",
    )
    parser.add_argument("--output-json", default=None, help="path to the output JSON summary file")
    parser.add_argument(
        "--attack-description",
        default="The final request has been confirmed as an attack request; keep only upstream database writes that truly carry or propagate the attack payload.",
        help="attack context provided to the LLM",
    )
    parser.add_argument(
        "--llm-mode",
        choices=["deepseek", "heuristic"],
        default="deepseek",
        help="deepseek uses the real LLM; heuristic uses offline heuristic decisions",
    )
    parser.add_argument("--model", default="deepseek-v4-flash", help="DeepSeek model name")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY", help="environment variable containing the API key")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions"),
        help="DeepSeek Chat Completions API URL",
    )
    parser.add_argument("--env-file", default=".env", help="path to the .env file used to load the API key")
    parser.add_argument(
        "--read-selection",
        choices=["earliest", "latest"],
        default="earliest",
        help="choose the earliest or latest read event when one request reads the same row multiple times",
    )
    parser.add_argument(
        "--max-candidates-per-read",
        type=int,
        default=None,
        help="maximum number of candidate write events to inspect before each read; unlimited by default",
    )
    parser.add_argument("--timeout-seconds", type=int, default=60, help="DeepSeek API timeout in seconds")
    parser.add_argument("--case-sensitive", action="store_true", help="use case-sensitive table-name matching")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    env_path = Path(args.env_file).expanduser()
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    load_env_file(env_path)

    read_log_path = Path(args.read_log).expanduser()
    write_log_path = Path(args.write_log).expanduser()
    output_html_path = Path(args.output_html).expanduser()
    output_json_path = Path(args.output_json).expanduser() if args.output_json else output_html_path.with_suffix(".json")

    reads = load_read_logs(read_log_path, case_sensitive=args.case_sensitive)
    writes = load_write_logs(write_log_path, case_sensitive=args.case_sensitive)
    writes_index, writes_time_index, request_read_events = build_indexes(reads, writes)

    if args.root_rid not in request_read_events:
        print(f"warning: root request {args.root_rid} has no read log records; output will contain only the root node.", file=sys.stderr)

    if args.llm_mode == "heuristic":
        analyzer: BaseTaintAnalyzer = HeuristicTaintAnalyzer()
    else:
        api_key = os.environ.get(args.api_key_env, "").strip()
        if not api_key:
            parser.error(
                f"API key not found; set environment variable {args.api_key_env} or provide it in .env; "
                "you can also use --llm-mode heuristic for offline validation."
            )
        analyzer = DeepSeekTaintAnalyzer(
            api_key=api_key,
            model_name=args.model,
            api_url=args.api_url,
            timeout_seconds=args.timeout_seconds,
        )

    req_parents, links, pruned_reads, decision_audit = build_taint_backtrace(
        root_rid=args.root_rid,
        attack_description=args.attack_description,
        analyzer=analyzer,
        writes_index=writes_index,
        writes_time_index=writes_time_index,
        request_read_events=request_read_events,
        read_selection=args.read_selection,
        max_candidates_per_read=args.max_candidates_per_read,
    )

    export_html_report(
        root_rid=args.root_rid,
        req_parents=req_parents,
        links=links,
        pruned_reads=pruned_reads,
        output_path=output_html_path,
    )
    write_summary_json(
        root_rid=args.root_rid,
        output_path=output_json_path,
        req_parents=req_parents,
        links=links,
        pruned_reads=pruned_reads,
        decision_audit=decision_audit,
    )

    paths = compute_paths(args.root_rid, req_parents)
    predecessor_requests = sorted({rid for path in paths for rid in path if rid != args.root_rid})

    print(f"total read events: {len(reads)}")
    print(f"total write events: {len(writes)}")
    print(f"retained taint dependency edges: {len(links)}")
    print(f"pruned data-read edges: {len(pruned_reads)}")
    print(f"predecessor request count: {len(predecessor_requests)}")
    print(f"HTML file written to: {output_html_path}")
    print(f"JSON summary written to: {output_json_path}")
    if paths:
        print("taint propagation paths:")
        for path in paths:
            print("  " + " <- ".join(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
