#!/usr/bin/env python3
"""Verify whether request_ids in cell_backtrace.json cover attack request IDs.

Compute Precision / Recall / F1. This replaces the old eval_dot.py approach
that extracted RIDs from dot files with regexes, and reads JSON directly.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def score(hit: int, predicted: int, gold: int) -> tuple[float, float, float]:
    precision = hit / predicted if predicted else 0.0
    recall = hit / gold if gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "Usage: eval_cell.py <cell_backtrace_json> <attack_rids_json> <out_json>",
            file=sys.stderr,
        )
        return 1

    bt_path = Path(sys.argv[1])
    attack_path = Path(sys.argv[2])
    out_path = Path(sys.argv[3])

    bt = json.loads(bt_path.read_text(encoding="utf-8"))
    attack = json.loads(attack_path.read_text(encoding="utf-8"))

    gold = {str(item["rid"]) for item in attack.get("rids", []) if item.get("rid")}
    root_rid = attack.get("root_rid", "")

    # request_ids are requests reached through certain backtrace edges; always include root_rid.
    pred = set(bt.get("request_ids", []))
    if root_rid:
        pred.add(root_rid)

    hit_set = pred & gold
    precision, recall, f1 = score(len(hit_set), len(pred), len(gold))

    # Count snapshot coverage information.
    links = bt.get("links", [])
    fallback_count = sum(
        1 for link in links if "fallback" in str(link.get("decision", {}).get("reason", "")).lower()
    )
    uncertain_count = sum(
        1 for link in links if link.get("certainty") == "uncertain"
    )

    result = {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "hit_count": len(hit_set),
        "predicted_count": len(pred),
        "attack_count": len(gold),
        "hit_rids": sorted(hit_set),
        "missing_attack_rids": sorted(gold - pred),
        "extra_dot_rids": sorted(pred - gold),
        "candidate_count": len(bt.get("candidate_request_ids", [])),
        "uncertain_count": len(bt.get("uncertain_request_ids", [])),
        "total_links": len(links),
        "fallback_links": fallback_count,
        "uncertain_links": uncertain_count,
        "unresolved_cells": len(bt.get("unresolved_cells", [])),
    }
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
