#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from analysis_cell.core import PrunePolicy, build_cell_trace, build_indexes
from analysis_cell.io_utils import load_read_log, load_write_log


SCENES = (
    "admidio",
    "airflow",
    "dolphinscheduler",
    "flowable",
    "gitlab14.1_fork",
    "gitlab14.1_xss",
    "gitlab8.13",
    "kanboard",
    "moodle",
    "ofbiz",
    "superset",
)


@dataclass(frozen=True)
class Parameters:
    q: float = 0.9
    table_request_floor: float = 0.2
    table_endpoint_floor: float = 0.3
    row_request_threshold: float = 0.2
    row_endpoint_floor: float = 0.3
    amplification_threshold: float = 3.0
    write_fanout_threshold: float = 3.0
    write_request_guard: float = 0.1


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    position = (len(values) - 1) * q
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default="runs/exp006")
    parser.add_argument(
        "--output", default="runs/exp006/parameter_sensitivity.json"
    )
    return parser.parse_args()


def load_scenes(run_dir: Path) -> dict:
    loaded = {}
    for scene in SCENES:
        scene_dir = run_dir / scene
        raw_policy = json.loads(
            (scene_dir / "pruning_policy.json").read_text(encoding="utf-8")
        )
        attack = json.loads(
            (scene_dir / "attack_rids.json").read_text(encoding="utf-8")
        )
        reads = load_read_log(scene_dir / "request_db_read.log")
        writes, deletes = load_write_log(scene_dir / "request_db_write.log")
        loaded[scene] = {
            "raw_policy": raw_policy,
            "indexes": build_indexes(reads, writes, deletes),
            "root_rid": str(attack["root_rid"]),
            "gold": {
                str(item["rid"])
                for item in attack.get("rids", [])
                if item.get("rid")
            },
        }
    return loaded


def classify(raw_policy: dict, parameters: Parameters) -> PrunePolicy:
    table_metrics = raw_policy["table_metrics"]
    row_metrics = raw_policy["row_metrics"]
    table_request_threshold = max(
        percentile(
            [item["request_coverage"] for item in table_metrics.values()],
            parameters.q,
        ),
        parameters.table_request_floor,
    )
    table_endpoint_threshold = max(
        percentile(
            [item["endpoint_coverage"] for item in table_metrics.values()],
            parameters.q,
        ),
        parameters.table_endpoint_floor,
    )
    row_endpoint_threshold = max(
        percentile(
            [item["endpoint_coverage"] for item in row_metrics.values()],
            parameters.q,
        ),
        parameters.row_endpoint_floor,
    )

    block_tables = frozenset(
        table
        for table, item in table_metrics.items()
        if item["request_coverage"] >= table_request_threshold
        and item["endpoint_coverage"] >= table_endpoint_threshold
        and item["amplification"] >= parameters.amplification_threshold
        and item["rw_balance"] > 0
        and item["write_request_coverage"] < parameters.write_request_guard
    )
    block_rows = set()
    gray_rows = set()
    for item in row_metrics.values():
        row = (item["table"], str(item["pk"]))
        if (
            item["request_coverage"] >= parameters.row_request_threshold
            and item["endpoint_coverage"] >= row_endpoint_threshold
        ):
            block_rows.add(row)
        elif item["write_fanout"] >= parameters.write_fanout_threshold:
            gray_rows.add(row)

    return PrunePolicy(
        block_tables=block_tables,
        block_rows=frozenset(block_rows),
        gray_rows=frozenset(gray_rows),
    )


def policy_key(policy: PrunePolicy) -> tuple:
    return (
        tuple(sorted(policy.block_tables)),
        tuple(sorted(policy.block_rows)),
        tuple(sorted(policy.gray_rows)),
    )


def score_scene(scene: dict, policy: PrunePolicy) -> dict:
    result = build_cell_trace(
        scene["root_rid"], scene["indexes"], policy=policy
    )
    predicted = result.request_ids
    gold = scene["gold"]
    true_positive = len(predicted & gold)
    precision = true_positive / len(predicted) if predicted else 0.0
    recall = true_positive / len(gold) if gold else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tp": true_positive,
        "fp": len(predicted - gold),
        "fn": len(gold - predicted),
        "predicted_request_count": len(predicted),
        "link_count": len(result.links),
        "block_suppressed": result.suppressed.get("block", 0),
        "gray_no_recurse": result.suppressed.get("gray-no-recurse", 0),
    }


def round_floats(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: round_floats(item) for key, item in value.items()}
    if isinstance(value, list):
        return [round_floats(item) for item in value]
    return value


def evaluate(
    loaded: dict,
    parameters: Parameters,
    trace_cache: dict,
) -> dict:
    per_scene = {}
    policy_totals = {"block_tables": 0, "block_rows": 0, "gray_rows": 0}
    for scene_name, scene in loaded.items():
        policy = classify(scene["raw_policy"], parameters)
        policy_totals["block_tables"] += len(policy.block_tables)
        policy_totals["block_rows"] += len(policy.block_rows)
        policy_totals["gray_rows"] += len(policy.gray_rows)
        cache_key = (scene_name, policy_key(policy))
        if cache_key not in trace_cache:
            trace_cache[cache_key] = score_scene(scene, policy)
        per_scene[scene_name] = trace_cache[cache_key]

    count = len(per_scene)
    macro = {
        metric: sum(item[metric] for item in per_scene.values()) / count
        for metric in ("precision", "recall", "f1")
    }
    graph_totals = {
        metric: sum(item[metric] for item in per_scene.values())
        for metric in (
            "predicted_request_count",
            "link_count",
            "block_suppressed",
            "gray_no_recurse",
        )
    }
    return round_floats(
        {
            "parameters": asdict(parameters),
            "macro": macro,
            "policy_totals": policy_totals,
            "graph_totals": graph_totals,
            "per_scene": per_scene,
        }
    )


def annotate(result: dict, baseline: dict) -> dict:
    result["delta_macro_f1"] = round(
        result["macro"]["f1"] - baseline["macro"]["f1"], 6
    )
    result["f1_changed_scenes"] = [
        scene
        for scene in SCENES
        if result["per_scene"][scene]["f1"]
        != baseline["per_scene"][scene]["f1"]
    ]
    result["graph_changed_scenes"] = [
        scene
        for scene in SCENES
        if any(
            result["per_scene"][scene][field]
            != baseline["per_scene"][scene][field]
            for field in (
                "predicted_request_count",
                "link_count",
                "block_suppressed",
                "gray_no_recurse",
            )
        )
    ]
    return result


def scan_one_at_a_time(loaded: dict, baseline: dict, trace_cache: dict) -> dict:
    defaults = Parameters()
    values = {
        "q": (0.8, 0.85, 0.9, 0.95, 0.99),
        "table_request_floor": (0.1, 0.2, 0.3, 0.5, 0.7),
        "table_endpoint_floor": (0.2, 0.3, 0.4, 0.5, 0.7),
        "row_request_threshold": (0.1, 0.15, 0.2, 0.25, 0.26, 0.28, 0.3),
        "row_endpoint_floor": (0.2, 0.3, 0.4, 0.5),
        "amplification_threshold": (2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0),
        "write_fanout_threshold": (2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 50.0, 100.0),
        "write_request_guard": (0.05, 0.06, 0.07, 0.075, 0.1, 0.15, 0.2, 0.3, 0.34, 0.342, 0.4),
    }
    output = {}
    for field, candidates in values.items():
        output[field] = []
        for value in candidates:
            parameters = replace(defaults, **{field: value})
            result = evaluate(loaded, parameters, trace_cache)
            result["value"] = value
            output[field].append(annotate(result, baseline))
    return output


def scan_joint(loaded: dict, baseline: dict, trace_cache: dict) -> dict:
    defaults = Parameters()
    output = {"amplification_x_write_guard": [], "row_coverage_x_fanout": []}
    for amplification in (2.0, 3.0, 4.0, 4.5, 5.0):
        for guard in (0.05, 0.075, 0.1, 0.2, 0.35):
            parameters = replace(
                defaults,
                amplification_threshold=amplification,
                write_request_guard=guard,
            )
            output["amplification_x_write_guard"].append(
                annotate(evaluate(loaded, parameters, trace_cache), baseline)
            )
    for coverage in (0.15, 0.2, 0.25, 0.28, 0.3):
        for fanout in (2.0, 3.0, 10.0, 50.0, 100.0):
            parameters = replace(
                defaults,
                row_request_threshold=coverage,
                write_fanout_threshold=fanout,
            )
            output["row_coverage_x_fanout"].append(
                annotate(evaluate(loaded, parameters, trace_cache), baseline)
            )
    return output


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir)
    loaded = load_scenes(run_dir)
    algorithms = sorted(
        {scene["raw_policy"]["algorithm"] for scene in loaded.values()}
    )
    trace_cache = {}
    baseline = evaluate(loaded, Parameters(), trace_cache)
    result = {
        "schema_version": 1,
        "experiment": run_dir.name,
        "source_policy_algorithms": algorithms,
        "scope": (
            "Frozen-metric sensitivity analysis: reclassifies tables/rows from "
            "saved metrics and reruns cell backtrace; it does not relearn "
            "amplification under different sample sizes or trace depths."
        ),
        "scenes": list(SCENES),
        "baseline": baseline,
        "one_at_a_time": scan_one_at_a_time(
            loaded, baseline, trace_cache
        ),
        "joint": scan_joint(loaded, baseline, trace_cache),
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {output_path}")
    print(json.dumps(result["baseline"]["macro"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
