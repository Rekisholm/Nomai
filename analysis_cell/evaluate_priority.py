from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any, Sequence

from .core import build_indexes
from .io_utils import load_read_log, load_request_context_log, load_write_log
from .priority import build_fifo_trace, build_priority_trace


def _load_gold(path: Path) -> tuple[str, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    gold = [
        str(item["rid"] if isinstance(item, dict) else item)
        for item in data.get("rids", [])
    ]
    root = str(data.get("root_rid") or (gold[-1] if gold else ""))
    return root, gold


def _first_request_ranks(result: Any) -> dict[str, int]:
    ranks = {result.root_rid: 0}
    for link in result.links:
        if link.certainty != "certain" or link.task_rank is None:
            continue
        ranks[link.parent_rid] = min(
            ranks.get(link.parent_rid, link.task_rank),
            link.task_rank,
        )
        ranks.setdefault(
            link.child_rid,
            0 if link.child_rid == result.root_rid else link.task_rank,
        )
    return ranks


def evaluate_scene(
    scene_dir: Path,
    alpha: float,
    beta: float,
) -> dict[str, Any]:
    root_rid, gold = _load_gold(scene_dir / "attack_rids.json")
    reads = load_read_log(scene_dir / "request_db_read.log")
    writes, deletes = load_write_log(scene_dir / "request_db_write.log")
    indexes = build_indexes(reads, writes, deletes)
    context_path = scene_dir / "request_context.log"
    request_contexts = (
        load_request_context_log(context_path)
        if context_path.is_file() and context_path.stat().st_size > 0
        else None
    )
    result = build_priority_trace(
        root_rid,
        indexes,
        alpha=alpha,
        beta=beta,
        request_contexts=request_contexts,
    )
    request_id_result = (
        build_priority_trace(
            root_rid, indexes, alpha=alpha, beta=beta,
        )
        if request_contexts is not None
        else result
    )
    fifo_result = build_fifo_trace(root_rid, indexes)
    ranks = _first_request_ranks(result)
    fifo_ranks = _first_request_ranks(fifo_result)
    request_id_ranks = _first_request_ranks(request_id_result)
    reachable_gold = [rid for rid in gold if rid in result.request_ids]
    unreachable_gold = [rid for rid in gold if rid not in result.request_ids]
    required_budget = max((ranks[rid] for rid in reachable_gold), default=0)
    full_budget = result.tasks_processed
    fifo_required_budget = max(
        (fifo_ranks[rid] for rid in reachable_gold),
        default=0,
    )
    request_id_required_budget = max(
        (request_id_ranks[rid] for rid in reachable_gold),
        default=0,
    )
    request_nodes_at_budget = sum(
        rank <= required_budget for rank in ranks.values()
    )
    fifo_request_nodes_at_budget = sum(
        rank <= fifo_required_budget for rank in fifo_ranks.values()
    )

    checkpoints: dict[str, Any] = {}
    for ratio in (0.01, 0.05, 0.10, 0.25):
        budget = min(full_budget, max(1, round(full_budget * ratio)))
        hit = sum(ranks.get(rid, full_budget + 1) <= budget for rid in gold)
        checkpoints[f"{int(ratio * 100)}%"] = {
            "budget": budget,
            "gold_hit": hit,
            "gold_recall": hit / len(gold) if gold else 0.0,
        }

    return {
        "scene": scene_dir.name,
        "root_rid": root_rid,
        "gold_count": len(gold),
        "reachable_gold_count": len(reachable_gold),
        "reachable_gold": reachable_gold,
        "unreachable_gold": unreachable_gold,
        "complete_graph_requests": len(result.request_ids),
        "complete_budget_tasks": full_budget,
        "diversity_mode": result.priority_diversity_mode,
        "request_contexts_loaded": result.request_contexts_loaded,
        "budget_for_all_reachable_gold": required_budget,
        "budget_reduction": (
            1.0 - required_budget / full_budget if full_budget else 0.0
        ),
        "fifo_budget_for_all_reachable_gold": fifo_required_budget,
        "fifo_budget_reduction": (
            1.0 - fifo_required_budget / full_budget if full_budget else 0.0
        ),
        "priority_vs_fifo_budget_delta": required_budget - fifo_required_budget,
        "request_id_budget_for_all_reachable_gold": request_id_required_budget,
        "context_vs_request_id_budget_delta": (
            required_budget - request_id_required_budget
        ),
        "request_nodes_at_gold_budget": request_nodes_at_budget,
        "request_node_reduction": (
            1.0 - request_nodes_at_budget / len(result.request_ids)
            if result.request_ids
            else 0.0
        ),
        "fifo_request_nodes_at_gold_budget": fifo_request_nodes_at_budget,
        "fifo_request_node_reduction": (
            1.0 - fifo_request_nodes_at_budget / len(result.request_ids)
            if result.request_ids
            else 0.0
        ),
        "gold_first_task_rank": {rid: ranks.get(rid) for rid in gold},
        "checkpoints": checkpoints,
    }


def evaluate_run(
    run_dir: Path,
    alpha: float,
    beta: float,
) -> dict[str, Any]:
    scenes = []
    for scene_dir in sorted(path for path in run_dir.iterdir() if path.is_dir()):
        required = (
            scene_dir / "attack_rids.json",
            scene_dir / "request_db_read.log",
            scene_dir / "request_db_write.log",
        )
        if all(path.exists() for path in required):
            scenes.append(evaluate_scene(scene_dir, alpha, beta))

    reductions = [item["budget_reduction"] for item in scenes]
    total_full = sum(item["complete_budget_tasks"] for item in scenes)
    total_required = sum(item["budget_for_all_reachable_gold"] for item in scenes)
    total_fifo_required = sum(
        item["fifo_budget_for_all_reachable_gold"] for item in scenes
    )
    total_complete_requests = sum(
        item["complete_graph_requests"] for item in scenes
    )
    total_request_nodes = sum(
        item["request_nodes_at_gold_budget"] for item in scenes
    )
    total_fifo_request_nodes = sum(
        item["fifo_request_nodes_at_gold_budget"] for item in scenes
    )
    return {
        "run_dir": str(run_dir),
        "alpha": alpha,
        "beta": beta,
        "scenes": scenes,
        "aggregate": {
            "scene_count": len(scenes),
            "gold_count": sum(item["gold_count"] for item in scenes),
            "reachable_gold_count": sum(
                item["reachable_gold_count"] for item in scenes
            ),
            "macro_mean_budget_reduction": (
                statistics.mean(reductions) if reductions else 0.0
            ),
            "median_budget_reduction": (
                statistics.median(reductions) if reductions else 0.0
            ),
            "weighted_budget_reduction": (
                1.0 - total_required / total_full if total_full else 0.0
            ),
            "complete_budget_tasks": total_full,
            "budget_for_all_reachable_gold": total_required,
            "fifo_budget_for_all_reachable_gold": total_fifo_required,
            "weighted_fifo_budget_reduction": (
                1.0 - total_fifo_required / total_full if total_full else 0.0
            ),
            "priority_beats_fifo_scenes": sum(
                item["priority_vs_fifo_budget_delta"] < 0 for item in scenes
            ),
            "priority_ties_fifo_scenes": sum(
                item["priority_vs_fifo_budget_delta"] == 0 for item in scenes
            ),
            "priority_loses_fifo_scenes": sum(
                item["priority_vs_fifo_budget_delta"] > 0 for item in scenes
            ),
            "complete_graph_requests": total_complete_requests,
            "request_nodes_at_gold_budget": total_request_nodes,
            "weighted_request_node_reduction": (
                1.0 - total_request_nodes / total_complete_requests
                if total_complete_requests
                else 0.0
            ),
            "fifo_request_nodes_at_gold_budget": total_fifo_request_nodes,
            "weighted_fifo_request_node_reduction": (
                1.0 - total_fifo_request_nodes / total_complete_requests
                if total_complete_requests
                else 0.0
            ),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate the cell-task budget needed for priority tracing to recover the attack chain"
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--alpha", type=float, default=0.5)
    parser.add_argument("--beta", type=float, default=0.5)
    parser.add_argument("--output", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.alpha < 0 or args.beta < 0 or abs(args.alpha + args.beta - 1.0) > 1e-9:
        raise SystemExit("--alpha/--beta must be non-negative and sum to 1")
    report = evaluate_run(Path(args.run_dir), args.alpha, args.beta)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"output={output}")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
