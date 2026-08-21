#!/usr/bin/env python3
"""Verify whether a dot file covers attack request IDs and compute Precision / Recall / F1."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


REQ_RE = re.compile(r"R:([^\"\\\n]+)")


def score(hit: int, predicted: int, gold: int) -> tuple[float, float, float]:
    precision = hit / predicted if predicted else 0.0
    recall = hit / gold if gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def main() -> int:
    if len(sys.argv) != 4:
        print("Usage: eval_dot.py <dot_path> <attack_rids_json> <out_json>", file=sys.stderr)
        return 1

    dot_path = Path(sys.argv[1])
    attack_path = Path(sys.argv[2])
    out_path = Path(sys.argv[3])

    dot_text = dot_path.read_text(encoding="utf-8", errors="ignore")
    predicted = {m.group(1).split("\\n", 1)[0].split("#", 1)[0] for m in REQ_RE.finditer(dot_text)}

    attack = json.loads(attack_path.read_text(encoding="utf-8"))
    gold = {str(item["rid"]) for item in attack.get("rids", []) if item.get("rid")}
    hit_set = predicted & gold
    precision, recall, f1 = score(len(hit_set), len(predicted), len(gold))

    result = {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "hit_count": len(hit_set),
        "predicted_count": len(predicted),
        "attack_count": len(gold),
        "hit_rids": sorted(hit_set),
        "missing_attack_rids": sorted(gold - predicted),
        "extra_dot_rids": sorted(predicted - gold),
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
