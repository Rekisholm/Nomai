from __future__ import annotations

import importlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Sequence

try:
    dtparser = importlib.import_module("dateutil.parser")
except ImportError:
    dtparser = None


def parse_timestamp(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        if dtparser is None:
            raise
        return dtparser.parse(text)


def normalize_table_name(value: str, case_sensitive: bool) -> str:
    table_name = value.strip()
    if "." in table_name:
        table_name = table_name.rsplit(".", 1)[-1]
    return table_name if case_sensitive else table_name.lower()


def stringify_pk(value: Any) -> str:
    return str(value)


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def cells_to_row(cells: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    row: Dict[str, Any] = {}
    for cell in cells:
        row[str(cell.get("col", ""))] = cell.get("new")
    return row


def flatten_row_text(row: Dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)


def dot_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def extract_json_object(text: str) -> Dict[str, Any]:
    content = text.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        if len(lines) >= 3:
            content = "\n".join(lines[1:-1]).strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))
