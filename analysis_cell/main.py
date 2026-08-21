from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from .analyzers import DeepSeekCellAnalyzer, HeuristicCellAnalyzer
from .core import (
    DEFAULT_ATTACK_DESCRIPTION,
    build_cell_trace,
    build_indexes,
    load_prune_policy,
)
from .io_utils import (
    load_env_file,
    load_read_log,
    load_request_context_log,
    load_write_log,
    write_dot,
    write_json,
)
from .priority import build_priority_trace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backtrace attack requests using database cell versions"
    )
    parser.add_argument("--read-log", required=True)
    parser.add_argument("--write-log", required=True)
    parser.add_argument("--root-rid", required=True)
    parser.add_argument(
        "--request-context-log",
        default=None,
        help=(
            "JSONL mapping rid to (user_id, api_id) for priority mode; "
            "when omitted, writers are distinguished by request_id"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("basic", "priority", "prune", "llm", "prune_llm"),
        default="basic",
        help=(
            "basic=complete tracing; priority=budgeted cell-priority tracing; "
            "prune=common table/row noise pruning; llm=per-cell LLM pruning; "
            "prune_llm=structural pruning followed by LLM pruning"
        ),
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=None,
        help="maximum cell tasks to process in priority mode; omit to exhaust the queue",
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=0.5,
        help="rarity weight for priority mode, default 0.5",
    )
    parser.add_argument(
        "--beta",
        type=float,
        default=0.5,
        help="expansion-cost weight for priority mode, default 0.5; alpha+beta must equal 1",
    )
    parser.add_argument(
        "--policy",
        default=None,
        help="path to pruning_policy.json, required for prune/prune_llm modes",
    )
    parser.add_argument(
        "--output-dot",
        default="causal_tree_with_cells.html",
        help="output path for the interactive HTML provenance graph",
    )
    parser.add_argument("--output-json", default="cell_backtrace.json")
    parser.add_argument(
        "--attack-description",
        default=DEFAULT_ATTACK_DESCRIPTION,
        help="context for LLM decisions; empty input uses the generic unknown-attack prompt",
    )
    parser.add_argument(
        "--llm-provider",
        choices=("deepseek", "heuristic"),
        default="deepseek",
        help="heuristic is only for offline smoke tests",
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--api-key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-url", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--case-sensitive", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.batch_size <= 0:
        parser.error("--batch-size must be greater than 0")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be greater than 0")
    if args.budget is not None and args.budget < 0:
        parser.error("--budget must be greater than or equal to 0")
    if args.mode != "priority" and args.budget is not None:
        parser.error("--budget is only valid with --mode priority")
    if args.mode != "priority" and args.request_context_log is not None:
        parser.error("--request-context-log is only valid with --mode priority")
    if args.mode == "priority":
        if args.alpha < 0 or args.beta < 0:
            parser.error("--alpha/--beta must be greater than or equal to 0")
        if abs(args.alpha + args.beta - 1.0) > 1e-9:
            parser.error("--alpha and --beta must sum to 1")

    env_path = Path(args.env_file).expanduser()
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    load_env_file(env_path)

    reads = load_read_log(args.read_log, case_sensitive=args.case_sensitive)
    writes, deletes = load_write_log(
        args.write_log, case_sensitive=args.case_sensitive
    )
    indexes = build_indexes(reads, writes, deletes)
    request_contexts = None
    if args.request_context_log is not None:
        request_contexts = load_request_context_log(args.request_context_log)
        print(
            f"loaded request contexts: {len(request_contexts)} "
            f"from {args.request_context_log}"
        )

    # --- Pruning policy (structural noise filter) ---
    policy = None
    needs_policy = args.mode in ("prune", "prune_llm")
    if needs_policy:
        if not args.policy:
            parser.error(f"--mode {args.mode} requires --policy pruning_policy.json")
        policy = load_prune_policy(args.policy)
        print(f"loaded prune policy: {len(policy.block_tables)} block_tables, "
              f"{len(policy.block_rows)} block_rows, "
              f"{len(policy.gray_rows)} gray_rows")

    # --- LLM analyzer ---
    analyzer = None
    needs_llm = args.mode in ("llm", "prune_llm")
    if needs_llm:
        if args.llm_provider == "heuristic":
            analyzer = HeuristicCellAnalyzer()
        else:
            api_key = os.environ.get(args.api_key_env, "").strip()
            if not api_key:
                parser.error(
                    f"{args.api_key_env} was not found; configure it in the environment or {env_path}"
                )
            analyzer = DeepSeekCellAnalyzer(
                api_key=api_key,
                model_name=(
                    args.model
                    or os.environ.get("DEEPSEEK_MODEL", "").strip()
                    or "deepseek-v4-flash"
                ),
                api_url=(
                    args.api_url
                    or os.environ.get(
                        "DEEPSEEK_API_URL",
                        "https://api.deepseek.com/chat/completions",
                    ).strip()
                    or "https://api.deepseek.com/chat/completions"
                ),
                timeout_seconds=args.timeout_seconds,
                batch_size=args.batch_size,
            )

    if args.root_rid not in indexes.request_reads:
        print(
            f"warning: root_rid={args.root_rid} has no read log; only the root request will be output.",
            file=sys.stderr,
        )

    if args.mode == "priority":
        result = build_priority_trace(
            args.root_rid,
            indexes,
            budget=args.budget,
            alpha=args.alpha,
            beta=args.beta,
            request_contexts=request_contexts,
        )
    else:
        result = build_cell_trace(
            args.root_rid,
            indexes,
            analyzer=analyzer,
            attack_description=args.attack_description,
            policy=policy,
        )
    write_dot(result, args.output_dot)
    write_json(result, args.output_json)

    print(f"mode={result.mode}")
    print(f"requests={len(result.request_ids)}")
    print(f"candidate_requests={len(result.graph_request_ids)}")
    print(f"cell_links={len(result.links)}")
    print(f"certain_links={sum(link.certainty == 'certain' for link in result.links)}")
    print(f"uncertain_links={sum(link.certainty == 'uncertain' for link in result.links)}")
    print(f"pruned_cells={len(result.pruned_cells)}")
    print(f"unresolved_cells={len(result.unresolved_cells)}")
    if result.mode == "priority":
        print(f"budget_limit={result.budget_limit}")
        print(f"tasks_processed={result.tasks_processed}")
        print(f"tasks_discovered={result.tasks_discovered}")
        print(f"pending_tasks={len(result.pending_tasks)}")
        print(f"queue_exhausted={result.queue_exhausted}")
        print(f"diversity_mode={result.priority_diversity_mode}")
        print(f"request_contexts_loaded={result.request_contexts_loaded}")
    if result.suppressed:
        print(f"suppressed={result.suppressed}")
    print(f"dot={Path(args.output_dot)}")
    print(f"json={Path(args.output_json)}")
    return 0
