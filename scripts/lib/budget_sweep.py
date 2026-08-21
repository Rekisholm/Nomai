#!/usr/bin/env python3
"""Budget sweep analysis for priority / FIFO / random under different budgets.

Outputs:
  - JSON: full metrics per scene and budget
  - Markdown table: report summary
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any, Sequence

from analysis_cell.core import build_indexes
from analysis_cell.io_utils import (
    load_read_log,
    load_request_context_log,
    load_write_log,
)
from analysis_cell.priority import (
    build_fifo_trace,
    build_priority_trace,
    build_random_trace,
)


def _load_gold(path: Path) -> tuple[str, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    gold = [
        str(item["rid"] if isinstance(item, dict) else item)
        for item in data.get("rids", [])
    ]
    root = str(data.get("root_rid") or (gold[-1] if gold else ""))
    return root, gold


def _prf(hit: int, predicted: int, gold: int) -> tuple[float, float, float]:
    p = hit / predicted if predicted else 0.0
    r = hit / gold if gold else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f1


def _predicted_at_budget(result, budget: int) -> set[str]:
    """Slice a full trace by task_rank <= budget and return visible requests."""
    pred = {result.root_rid}
    for link in result.links:
        if link.certainty != "certain":
            continue
        if link.task_rank is not None and link.task_rank <= budget:
            pred.add(link.parent_rid)
            pred.add(link.child_rid)
    return pred


def _sweep_one_strategy(result, gold: list[str], full_budget: int) -> dict:
    gold_set = set(gold)
    reachable_gold = [rid for rid in gold if rid in result.request_ids]
    first_rank = {result.root_rid: 0}
    for link in result.links:
        if link.certainty != "certain" or link.task_rank is None:
            continue
        first_rank[link.parent_rid] = min(
            first_rank.get(link.parent_rid, link.task_rank),
            link.task_rank,
        )
    b_complete = max(
        (first_rank[rid] for rid in reachable_gold),
        default=0,
    )
    # Select budget checkpoints.
    pct_checkpoints = [0.01, 0.05, 0.10, 0.25, 0.50, 0.75, 1.0]
    abs_checkpoints = [1, 5, 10, 25, 50, 100, 200, 500, 1000]
    budgets = sorted(
        set(
            [0]
            + [b_complete]
            + [min(full_budget, max(1, round(full_budget * p))) for p in pct_checkpoints]
            + [b for b in abs_checkpoints if b <= full_budget]
            + [full_budget]
        )
    )

    curve = []
    for b in budgets:
        pred = _predicted_at_budget(result, b)
        hit = len(pred & gold_set)
        p, r, f1 = _prf(hit, len(pred), len(gold_set))
        curve.append({
            "budget": b,
            "budget_pct": b / full_budget if full_budget else 0.0,
            "predicted": len(pred),
            "hit": hit,
            "precision": round(p, 4),
            "recall": round(r, 4),
            "f1": round(f1, 4),
        })

    # AUC (trapezoidal, normalized to [0,1])
    f1_auc = _trapezoid_auc(
        [c["budget"] for c in curve],
        [c["f1"] for c in curve],
    )
    recall_auc = _trapezoid_auc(
        [c["budget"] for c in curve],
        [c["recall"] for c in curve],
    )

    return {
        "full_budget": full_budget,
        "reachable_gold": reachable_gold,
        "b_complete": b_complete,
        "budget_reduction": 1.0 - b_complete / full_budget if full_budget else 0.0,
        "f1_auc": round(f1_auc, 4),
        "recall_auc": round(recall_auc, 4),
        "curve": curve,
    }


def _trapezoid_auc(x: list[float], y: list[float]) -> float:
    if len(x) < 2:
        return 0.0
    x0, xN = x[0], x[-1]
    if xN == x0:
        return 0.0
    area = 0.0
    for i in range(len(x) - 1):
        area += (x[i + 1] - x[i]) * (y[i] + y[i + 1]) / 2.0
    return area / (xN - x0)


def evaluate_scene(scene_dir: Path, alpha: float, beta: float, n_seeds: int = 10) -> dict:
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

    # Full traces.
    pri_result = build_priority_trace(
        root_rid,
        indexes,
        alpha=alpha,
        beta=beta,
        request_contexts=request_contexts,
    )
    pri_v1_result = (
        build_priority_trace(root_rid, indexes, alpha=alpha, beta=beta)
        if request_contexts is not None
        else None
    )
    fifo_result = build_fifo_trace(root_rid, indexes)
    random_results = [
        build_random_trace(root_rid, indexes, random_seed=s)
        for s in range(n_seeds)
    ]

    full_budget = pri_result.tasks_processed
    fifo_full = fifo_result.tasks_processed

    pri_sweep = _sweep_one_strategy(pri_result, gold, full_budget)
    pri_v1_sweep = (
        _sweep_one_strategy(pri_v1_result, gold, pri_v1_result.tasks_processed)
        if pri_v1_result is not None
        else None
    )
    fifo_sweep = _sweep_one_strategy(fifo_result, gold, fifo_full)

    # Random: average across multiple seeds.
    random_curves = []
    random_b_completes = []
    random_aucs_f1 = []
    random_aucs_recall = []
    for rr in random_results:
        sweep = _sweep_one_strategy(rr, gold, rr.tasks_processed)
        random_curves.append(sweep["curve"])
        random_b_completes.append(sweep["b_complete"])
        random_aucs_f1.append(sweep["f1_auc"])
        random_aucs_recall.append(sweep["recall_auc"])

    # Align random curves and average them by budget_pct bin.
    random_avg_curve = _average_curves(random_curves)

    return {
        "scene": scene_dir.name,
        "gold_count": len(gold),
        "root_rid": root_rid,
        "full_budget_tasks": full_budget,
        "complete_graph_requests": len(pri_result.request_ids),
        "diversity_mode": pri_result.priority_diversity_mode,
        "request_contexts_loaded": pri_result.request_contexts_loaded,
        "priority": pri_sweep,
        "priority_v1": pri_v1_sweep,
        "fifo": fifo_sweep,
        "random": {
            "n_seeds": n_seeds,
            "b_complete_mean": round(statistics.mean(random_b_completes), 1) if random_b_completes else 0,
            "b_complete_std": round(statistics.stdev(random_b_completes), 1) if len(random_b_completes) > 1 else 0,
            "f1_auc_mean": round(statistics.mean(random_aucs_f1), 4) if random_aucs_f1 else 0,
            "f1_auc_std": round(statistics.stdev(random_aucs_f1), 4) if len(random_aucs_f1) > 1 else 0,
            "recall_auc_mean": round(statistics.mean(random_aucs_recall), 4) if random_aucs_recall else 0,
            "recall_auc_std": round(statistics.stdev(random_aucs_recall), 4) if len(random_aucs_recall) > 1 else 0,
            "budget_reduction_mean": round(
                1.0 - statistics.mean(random_b_completes) / full_budget if full_budget and random_b_completes else 0.0, 4
            ),
            "avg_curve": random_avg_curve,
        },
    }


def _average_curves(curves: list[list[dict]]) -> list[dict]:
    """Average multiple curves by budget_pct bin."""
    if not curves:
        return []
    # Use the first curve's budget_pct values as the reference.
    base_pcts = [c["budget_pct"] for c in curves[0]]
    result = []
    for i, pct in enumerate(base_pcts):
        vals = {"precision": [], "recall": [], "f1": [], "predicted": [], "hit": []}
        for curve in curves:
            if i < len(curve):
                for key in vals:
                    vals[key].append(curve[i][key])
        result.append({
            "budget_pct": round(pct, 4),
            "precision": round(statistics.mean(vals["precision"]), 4) if vals["precision"] else 0,
            "recall": round(statistics.mean(vals["recall"]), 4) if vals["recall"] else 0,
            "f1": round(statistics.mean(vals["f1"]), 4) if vals["f1"] else 0,
            "predicted": round(statistics.mean(vals["predicted"]), 1) if vals["predicted"] else 0,
            "hit": round(statistics.mean(vals["hit"]), 1) if vals["hit"] else 0,
        })
    return result


def evaluate_run(run_dir: Path, alpha: float, beta: float, n_seeds: int) -> dict:
    scenes = []
    for scene_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        required = (
            scene_dir / "attack_rids.json",
            scene_dir / "request_db_read.log",
            scene_dir / "request_db_write.log",
        )
        if not all(p.exists() for p in required):
            continue
        print(f"  sweeping {scene_dir.name}...", flush=True)
        scenes.append(evaluate_scene(scene_dir, alpha, beta, n_seeds))

    return {
        "run_dir": str(run_dir),
        "alpha": alpha,
        "beta": beta,
        "n_seeds": n_seeds,
        "scenes": scenes,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Budget sweep analysis")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--alpha", type=float, default=0.5)
    parser.add_argument("--beta", type=float, default=0.5)
    parser.add_argument("--n-seeds", type=int, default=10, help="number of random baseline seeds")
    parser.add_argument("--output", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if abs(args.alpha + args.beta - 1.0) > 1e-9:
        raise SystemExit("--alpha/--beta must be non-negative and sum to 1")
    report = evaluate_run(Path(args.run_dir), args.alpha, args.beta, args.n_seeds)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"output={out}")
    else:
        print(text[:200] + "...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
