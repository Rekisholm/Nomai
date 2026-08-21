#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from analysis_cell.core import (
    PrunePolicy,
    build_cell_trace,
    build_indexes,
    load_prune_policy,
)
from analysis_cell.io_utils import load_read_log, load_write_log

SCENES = [
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
]

RUN_DIR = _PROJECT_ROOT / "runs" / "exp006"

# Ablation variants: disable one rule at a time.
ABLATION_VARIANTS = {
    "full": lambda p: p,
    "no_table_block": lambda p: replace(p, block_tables=frozenset()),
    "no_row_block": lambda p: replace(p, block_rows=frozenset()),
    "no_row_gray": lambda p: replace(p, gray_rows=frozenset()),
    "no_rows": lambda p: replace(p, block_rows=frozenset(), gray_rows=frozenset()),
    "table_block_only": lambda p: replace(
        p, block_rows=frozenset(), gray_rows=frozenset()
    ),
}

VARIANT_ORDER = [
    "basic",
    "full",
    "no_table_block",
    "no_row_block",
    "no_row_gray",
    "no_rows",
    "table_block_only",
]

SHORT_NAMES = {
    "basic": "basic",
    "full": "full",
    "no_table_block": "no_tb",
    "no_row_block": "no_rb",
    "no_row_gray": "no_rg",
    "no_rows": "no_rows",
    "table_block_only": "tbl_b",
}


def score(hit: int, predicted: int, gold: int) -> dict:
    precision = hit / predicted if predicted else 0.0
    recall = hit / gold if gold else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "tp": hit,
        "fp": predicted - hit,
        "predicted": predicted,
        "gold": gold,
    }


def load_gold(scene_dir: Path) -> set[str]:
    data = json.loads((scene_dir / "attack_rids.json").read_text(encoding="utf-8"))
    return {str(item["rid"]) for item in data.get("rids", []) if item.get("rid")}


def get_root_rid(scene_dir: Path) -> str:
    data = json.loads((scene_dir / "attack_rids.json").read_text(encoding="utf-8"))
    return str(data["root_rid"])


def run_scene(scene: str) -> dict:
    scene_dir = RUN_DIR / scene
    if not (scene_dir / "pruning_policy.json").exists():
        return {"error": "no pruning_policy.json"}

    gold = load_gold(scene_dir)
    root_rid = get_root_rid(scene_dir)

    reads = load_read_log(str(scene_dir / "request_db_read.log"))
    writes, deletes = load_write_log(str(scene_dir / "request_db_write.log"))
    indexes = build_indexes(reads, writes, deletes)

    base_policy = load_prune_policy(str(scene_dir / "pruning_policy.json"))

    # Extract the actual thresholds.
    raw_policy = json.loads((scene_dir / "pruning_policy.json").read_text())
    thresholds = raw_policy.get("thresholds", {})

    policy_stats = {
        "block_tables": sorted(base_policy.block_tables),
        "block_rows": sorted(base_policy.block_rows),
        "gray_rows": sorted(base_policy.gray_rows),
        "block_table_count": len(base_policy.block_tables),
        "block_row_count": len(base_policy.block_rows),
        "gray_row_count": len(base_policy.gray_rows),
    }

    results = {}

    # Basic baseline without pruning.
    basic_result = build_cell_trace(root_rid, indexes, policy=None)
    basic_predicted = basic_result.request_ids
    basic_hit = basic_predicted & gold
    results["basic"] = {
        **score(len(basic_hit), len(basic_predicted), len(gold)),
        "hit_rids": sorted(basic_hit),
        "missing": sorted(gold - basic_predicted),
        "extra": sorted(basic_predicted - gold),
    }

    for variant_name, transform in ABLATION_VARIANTS.items():
        policy = transform(base_policy)
        result = build_cell_trace(root_rid, indexes, policy=policy)
        predicted = result.request_ids
        hit = predicted & gold
        results[variant_name] = {
            **score(len(hit), len(predicted), len(gold)),
            "hit_rids": sorted(hit),
            "missing": sorted(gold - predicted),
            "extra": sorted(predicted - gold),
            "suppressed": result.suppressed,
        }

    return {
        "gold": sorted(gold),
        "gold_count": len(gold),
        "thresholds": thresholds,
        "policy": policy_stats,
        "variants": results,
    }


def main() -> int:
    all_results = {}
    for scene in SCENES:
        scene_dir = RUN_DIR / scene
        if not scene_dir.exists():
            print(f"[skip] {scene}: directory not found")
            continue
        print(f"[run] {scene} ...")
        all_results[scene] = run_scene(scene)

    output_path = RUN_DIR / "ablation_result.json"
    output_path.write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\nResults written to {output_path}")

    # === Summary table 1: F1 by variant ===
    print("\n" + "=" * 110)
    header = f"{'Scene':<22} {'gold':>4}"
    for vk in VARIANT_ORDER:
        header += f"  {SHORT_NAMES[vk]:>8}"
    print(header)
    print("-" * 110)

    all_f1 = defaultdict(float)
    count = 0
    for scene in SCENES:
        if scene not in all_results or "error" in all_results[scene]:
            continue
        data = all_results[scene]
        row = f"{scene:<22} {data['gold_count']:>4}"
        for vk in VARIANT_ORDER:
            v = data["variants"].get(vk, {})
            f1 = v.get("f1", 0.0)
            row += f"  {f1:>8.3f}"
            if vk != "basic":
                all_f1[vk] += f1
        print(row)
        count += 1

    if count > 0:
        print("-" * 110)
        row = f"{'MEAN':<22} {'':>4}"
        for vk in VARIANT_ORDER:
            if vk == "basic":
                row += f"  {'':>8}"
            else:
                row += f"  {all_f1[vk] / count:>8.3f}"
        print(row)

    # === Summary table 2: Delta F1 (full - variant) ===
    print("\n" + "-" * 110)
    print("Delta F1 (full - variant), positive=worse without the rule, negative=better without the rule")
    print("-" * 110)
    delta_keys = ["no_table_block", "no_row_block", "no_row_gray", "no_rows", "table_block_only"]
    header = f"{'Scene':<22}"
    for vk in delta_keys:
        header += f"  {SHORT_NAMES[vk]:>8}"
    print(header)
    print("-" * 110)

    sum_delta = defaultdict(float)
    for scene in SCENES:
        if scene not in all_results or "error" in all_results[scene]:
            continue
        data = all_results[scene]
        full_f1 = data["variants"]["full"]["f1"]
        row = f"{scene:<22}"
        for vk in delta_keys:
            d = full_f1 - data["variants"][vk]["f1"]
            row += f"  {d:>+8.3f}"
            sum_delta[vk] += d
        print(row)

    if count > 0:
        print("-" * 110)
        row = f"{'MEAN':<22}"
        for vk in delta_keys:
            row += f"  {sum_delta[vk] / count:>+8.3f}"
        print(row)

    # === Summary table 3: thresholds and policy sizes ===
    print("\n" + "-" * 110)
    print("Thresholds and policy sizes")
    print("-" * 110)
    print(f"{'Scene':<22} {'tc':>6} {'te':>6} {'rc':>6} {'re':>6} {'am':>6} {'wf':>6}  | {'blk_t':>5} {'blk_r':>5} {'gry_r':>5}")
    print("-" * 110)
    for scene in SCENES:
        if scene not in all_results or "error" in all_results[scene]:
            continue
        data = all_results[scene]
        th = data["thresholds"]
        ps = data["policy"]
        print(f"{scene:<22} {th.get('table_high_coverage',0):>6.2f} {th.get('table_high_endpoint_coverage',0):>6.2f} "
              f"{th.get('row_high_coverage',0):>6.2f} {th.get('row_high_endpoint_coverage',0):>6.2f} "
              f"{th.get('high_amplification',0):>6.1f} {th.get('high_write_fanout',0):>6.1f}  | "
              f"{ps['block_table_count']:>5} {ps['block_row_count']:>5} {ps['gray_row_count']:>5}")

    print("=" * 110)
    print("Legend: no_tb=without table-level BLOCK  no_rb=without row-level BLOCK  "
          "no_rg=without row-level GRAY  no_rows=without all row-level rules  "
          "tbl_b=table-level BLOCK only")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
