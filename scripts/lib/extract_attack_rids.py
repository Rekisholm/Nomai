#!/usr/bin/env python3
"""Extract X-Request-Id values from attack script output and save them as JSON."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


RID_RE = re.compile(
    r"(?:X-Request[-_ ]?Id|Request[-_ ]?Id)"
    r"\s*[:=]?\s*([A-Za-z0-9][A-Za-z0-9_.:-]{1,})",
    re.IGNORECASE,
)

ARROW_RE = re.compile(
    r"->\s*\d{3}\s+([A-Za-z0-9][A-Za-z0-9_.:-]{1,})"
)


def clean(value: str) -> str | None:
    value = value.strip().strip(",;)")
    if not value or value.lower() in {"none", "null"}:
        return None
    if value.lower().startswith(("x-request", "request")):
        return None
    if value.isdigit() and len(value) <= 3:
        return None
    return value


def main() -> int:
    if len(sys.argv) != 4:
        print("Usage: extract_attack_rids.py <raw_log> <out_json> <scene>", file=sys.stderr)
        return 1

    raw_log = Path(sys.argv[1])
    out_json = Path(sys.argv[2])
    scene = sys.argv[3]

    rids: list[dict[str, object]] = []
    seen: set[str] = set()

    for lineno, line in enumerate(raw_log.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        matches = [m.group(1) for m in RID_RE.finditer(line)]
        matches.extend(m.group(1) for m in ARROW_RE.finditer(line))
        for value in matches:
            rid = clean(value)
            if not rid or rid in seen:
                continue
            seen.add(rid)
            rids.append({"rid": rid, "line": lineno, "text": line})

    payload = {
        "scene": scene,
        "root_rid": rids[-1]["rid"] if rids else None,
        "rids": rids,
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"attack_rids={out_json}")
    print(f"root_rid={payload['root_rid']}")
    print(f"rid_count={len(rids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
