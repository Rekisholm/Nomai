from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from dateutil import parser as date_parser

from .models import (
    CellWrite,
    ReadEvent,
    RequestContext,
    RowDelete,
    TraceResult,
)


def normalize_table_name(value: Any, case_sensitive: bool) -> str:
    name = str(value).strip()
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    return name if case_sensitive else name.lower()


def normalize_column_name(value: Any, case_sensitive: bool) -> str:
    name = str(value).strip()
    return name if case_sensitive else name.lower()


def parse_time(value: Any) -> datetime:
    return date_parser.parse(str(value))


def _optional_int(record: dict[str, Any], field: str, line_number: int) -> int | None:
    value = record.get(field)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"line {line_number}: {field} is not an integer: {value!r}"
        ) from error


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_request_context_log(path: str | Path) -> dict[str, RequestContext]:
    """Load Superset's offline rid -> (user, API endpoint) association log."""
    contexts: dict[str, RequestContext] = {}
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            record = json.loads(raw_line)
            rid = str(record.get("rid", "")).strip()
            api_id = str(record.get("api_id", "")).strip()
            if not api_id:
                method = str(record.get("method", "")).strip().upper()
                route = str(record.get("route", record.get("path", ""))).strip()
                if method and route:
                    api_id = f"{method} {route}"
            user_id = str(record.get("user_id", "")).strip()
            if not rid or not api_id or not user_id:
                raise ValueError(
                    f"request context log line {line_number} is missing rid/api_id/user_id"
                )
            context = RequestContext(
                rid=rid,
                api_id=api_id,
                user_id=user_id,
                identity_source=str(record.get("identity_source", "unknown")),
            )
            previous = contexts.get(rid)
            if previous is not None and (
                previous.api_id != context.api_id
                or previous.user_id != context.user_id
            ):
                raise ValueError(
                    f"request context log line {line_number} conflicts with another record for the same RID: {rid}"
                )
            contexts[rid] = context
    return contexts


def _record_pks(record: dict[str, Any], line_number: int) -> list[str]:
    if record.get("pk") is not None:
        return [str(record["pk"])]
    pks = record.get("pks")
    if pks is None:
        raise ValueError(f"line {line_number}: missing pk/pks")
    return [str(pk) for pk in pks]


def _parse_cells(raw_cells: Any, case_sensitive: bool, line_number: int) -> dict[str, Any]:
    if isinstance(raw_cells, dict):
        return {
            normalize_column_name(column, case_sensitive): value
            for column, value in raw_cells.items()
        }
    if not isinstance(raw_cells, list):
        raise ValueError(f"line {line_number}: cells must be an array or object")

    cells: dict[str, Any] = {}
    for cell in raw_cells:
        if not isinstance(cell, dict) or not str(cell.get("col", "")).strip():
            raise ValueError(f"line {line_number}: invalid cell entry: {cell!r}")
        if "new" not in cell and "value" not in cell:
            raise ValueError(f"line {line_number}: cell is missing new/value: {cell!r}")
        column = normalize_column_name(cell["col"], case_sensitive)
        cells[column] = cell["new"] if "new" in cell else cell["value"]
    return cells


def load_read_log(path: str | Path, case_sensitive: bool = False) -> list[ReadEvent]:
    reads: list[ReadEvent] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            record = json.loads(raw_line)
            rid = str(record.get("rid", record.get("request_id", ""))).strip()
            table = record.get("tbn", record.get("table_name"))
            timestamp = record.get("time", record.get("timestamp"))
            if not rid or table is None or timestamp is None:
                raise ValueError(f"read log line {line_number} is missing rid/tbn/time")

            xmin = _optional_int(record, "xmin", line_number)
            xmax = _optional_int(record, "xmax", line_number)
            if (xmin is None) != (xmax is None):
                raise ValueError(
                    f"read log line {line_number} must contain both xmin and xmax"
                )
            if xmin is not None and xmin > xmax:
                raise ValueError(
                    f"read log line {line_number} has invalid snapshot bounds: xmin={xmin}, xmax={xmax}"
                )
            read_sequence = _optional_int(record, "seq", line_number)
            if read_sequence is None:
                read_sequence = len(reads)

            pks = _record_pks(record, line_number)
            observed_cells = None
            if "cells" in record:
                if len(pks) != 1:
                    raise ValueError(
                        f"read log line {line_number} contains multiple pks and cells, so cells cannot be mapped to a specific row"
                    )
                observed_cells = _parse_cells(record["cells"], case_sensitive, line_number)

            for pk in pks:
                reads.append(ReadEvent(
                    time=parse_time(timestamp),
                    rid=rid,
                    table=normalize_table_name(table, case_sensitive),
                    pk=pk,
                    sequence=read_sequence,
                    xmin=xmin,
                    xmax=xmax,
                    observed_cells=observed_cells,
                ))
    reads.sort(key=lambda event: (event.rid, event.sequence, event.time))
    return reads


def load_write_log(
    path: str | Path,
    case_sensitive: bool = False,
) -> tuple[list[CellWrite], list[RowDelete]]:
    writes: list[CellWrite] = []
    deletes: list[RowDelete] = []
    event_sequence = 0
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            record = json.loads(raw_line)
            rid = str(record.get("rid", record.get("request_id", ""))).strip()
            table = record.get("tbn", record.get("table_name"))
            timestamp = record.get("time", record.get("timestamp"))
            operation = str(record.get("event", record.get("op", "update"))).lower()
            if not rid or table is None or timestamp is None:
                raise ValueError(f"write log line {line_number} is missing rid/tbn/time")
            if operation not in {"insert", "update", "delete"}:
                raise ValueError(
                    f"write log line {line_number} contains unknown event/op: {operation!r}"
                )

            pks = _record_pks(record, line_number)
            normalized_table = normalize_table_name(table, case_sensitive)
            event_time = parse_time(timestamp)
            xid = _optional_int(record, "xid", line_number)
            source_sequence = _optional_int(record, "seq", line_number)
            if source_sequence is None:
                source_sequence = event_sequence
            if operation == "delete":
                for pk in pks:
                    deletes.append(RowDelete(
                        event_time,
                        rid,
                        normalized_table,
                        pk,
                        source_sequence,
                        xid,
                    ))
                    event_sequence += 1
                continue

            if "cells" not in record:
                raise ValueError(
                    f"write log line {line_number} has no cells; this is a legacy row-level log "
                    "and cannot be used for cell-level provenance"
                )
            if len(pks) != 1:
                raise ValueError(
                    f"write log line {line_number} contains multiple pks and one cells set, "
                    "so each cell cannot be mapped to a specific row"
                )
            cells = _parse_cells(record["cells"], case_sensitive, line_number)
            for column, value in cells.items():
                writes.append(CellWrite(
                    time=event_time,
                    rid=rid,
                    operation=operation,
                    table=normalized_table,
                    pk=pks[0],
                    column=column,
                    value=value,
                    sequence=source_sequence,
                    xid=xid,
                ))
                event_sequence += 1

    writes.sort(key=lambda event: (event.time, event.sequence))
    deletes.sort(key=lambda event: (event.time, event.sequence))
    return writes, deletes


def _json_value(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _cell_name(cell: tuple[str, str, str]) -> str:
    return f"{cell[0]}[{cell[1]}].{cell[2]}"


def result_to_dict(result: TraceResult) -> dict[str, Any]:
    def decision_dict(decision: Any) -> dict[str, Any]:
        return {
            "tainted": decision.tainted,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "evidence": list(decision.evidence),
            "model": decision.model,
        }

    links = []
    for link in result.links:
        links.append({
            "parent_rid": link.parent_rid,
            "child_rid": link.child_rid,
            "cell": {
                "table": link.cell[0],
                "pk": link.cell[1],
                "column": link.cell[2],
                "name": _cell_name(link.cell),
                "value": _json_value(link.value),
            },
            "read_time": link.read_event.time.isoformat(),
            "read_snapshot": {
                "xmin": link.read_event.xmin,
                "xmax": link.read_event.xmax,
                "seq": link.read_event.sequence,
            },
            "latest_write": {
                "rid": link.latest_write.rid,
                "time": link.latest_write.time.isoformat(),
                "operation": link.latest_write.operation,
                "xid": link.latest_write.xid,
                "seq": link.latest_write.sequence,
            },
            "origin_write": {
                "rid": link.origin_write.rid,
                "time": link.origin_write.time.isoformat(),
                "operation": link.origin_write.operation,
                "xid": link.origin_write.xid,
            },
            "certainty": link.certainty,
            "resolution_reason": link.resolution_reason,
            "alternate_write": (
                {
                    "rid": link.alternate_write.rid,
                    "xid": link.alternate_write.xid,
                    "seq": link.alternate_write.sequence,
                    "value": _json_value(link.alternate_write.value),
                }
                if link.alternate_write is not None
                else None
            ),
            "decision": decision_dict(link.decision),
            "priority": (
                {
                    "score": link.task_priority,
                    "rank": link.task_rank,
                }
                if link.task_priority is not None
                else None
            ),
        })

    pruned = []
    for item in result.pruned_cells:
        candidate = item.candidate
        pruned.append({
            "child_rid": candidate.child_rid,
            "cell": _cell_name(candidate.cell),
            "value": _json_value(candidate.value),
            "origin_rid": candidate.origin_write.rid,
            "read_time": candidate.read_event.time.isoformat(),
            "decision": decision_dict(item.decision),
        })

    unresolved = [
        {
            "child_rid": item.child_rid,
            "table": item.row[0],
            "pk": item.row[1],
            "column": item.column,
            "read_time": item.read_time.isoformat(),
            "reason": item.reason,
        }
        for item in result.unresolved_cells
    ]
    pending_tasks = [
        {
            "child_rid": item.child_rid,
            "cell": _cell_name(item.cell),
            "writer_rids": list(item.writer_rids),
            "read_time": item.read_time.isoformat(),
            "read_sequence": item.read_sequence,
            "writer_diversity": item.writer_diversity,
            "writer_read_fanout": item.writer_read_fanout,
            "writer_identities": [
                {"user_id": user_id, "api_id": api_id}
                for user_id, api_id in item.writer_identities
            ],
            "rarity_score": item.rarity_score,
            "cost_score": item.cost_score,
            "write_history_count": item.write_history_count,
            "child_table_rows": item.child_table_rows,
            "child_table_cells": item.child_table_cells,
            "history_penalty": item.history_penalty,
            "scan_penalty": item.scan_penalty,
            "compactness_bonus": item.compactness_bonus,
            "base_priority": item.base_priority,
            "priority": item.priority,
        }
        for item in result.pending_tasks
    ]
    return {
        "root_rid": result.root_rid,
        "mode": result.mode,
        "request_ids": sorted(result.request_ids),
        "candidate_request_ids": sorted(result.graph_request_ids),
        "uncertain_request_ids": sorted(result.uncertain_request_ids),
        "visited_requests": sorted(result.visited_requests),
        "links": links,
        "pruned_cells": pruned,
        "unresolved_cells": unresolved,
        "suppressed": result.suppressed,
        "priority_search": (
            {
                "budget_limit": result.budget_limit,
                "tasks_processed": result.tasks_processed,
                "tasks_discovered": result.tasks_discovered,
                "pending_count": len(result.pending_tasks),
                "queue_exhausted": result.queue_exhausted,
                "alpha": result.priority_alpha,
                "beta": result.priority_beta,
                "diversity_mode": result.priority_diversity_mode,
                "request_contexts_loaded": result.request_contexts_loaded,
                "pending_tasks": pending_tasks,
            }
            if result.mode == "priority"
            else None
        ),
        "counts": {
            "requests": len(result.request_ids),
            "candidate_requests": len(result.graph_request_ids),
            "links": len(links),
            "certain_links": sum(link.certainty == "certain" for link in result.links),
            "uncertain_links": sum(link.certainty == "uncertain" for link in result.links),
            "pruned_cells": len(pruned),
            "unresolved_cells": len(unresolved),
        },
    }


def write_json(result: TraceResult, path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result_to_dict(result), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _cell_data(link: Any) -> dict[str, Any]:
    table, pk, column = link.cell
    decision = link.decision
    return {
        "name": f"{table}[{pk}].{column}",
        "nodeType": "cell",
        "table": table,
        "pk": pk,
        "column": column,
        "value": _json_value(link.value),
        "operation": link.origin_write.operation,
        "origin_rid": link.origin_write.rid,
        "write_time": link.origin_write.time.isoformat(),
        "read_time": link.read_event.time.isoformat(),
        "tainted": decision.tainted,
        "confidence": decision.confidence,
        "reason": decision.reason,
        "evidence": list(decision.evidence),
        "model": decision.model,
        "certainty": link.certainty,
        "resolution_reason": link.resolution_reason,
        "xmin": link.read_event.xmin,
        "xmax": link.read_event.xmax,
        "write_xid": link.latest_write.xid,
        "alternate_rid": (
            link.alternate_write.rid if link.alternate_write is not None else None
        ),
        "task_priority": link.task_priority,
        "task_rank": link.task_rank,
    }


def _build_cell_tree(result: TraceResult) -> dict[str, Any]:
    """Build a flat DAG where each request RID appears exactly once.

    Multiple cells between the same (reader, writer) pair collapse into a single
    ``cell-group`` node. Returns a structure of unique requests, unique cell
    groups, and layer (depth) info for layout. No request is duplicated.
    """
    links_by_child: dict[str, list[Any]] = {}
    for link in sorted(
        result.links,
        key=lambda item: (
            item.parent_rid,
            item.cell,
            min(item.read_event.time, item.origin_write.time),
        ),
    ):
        links_by_child.setdefault(link.child_rid, []).append(link)

    requests: dict[str, dict[str, Any]] = {}
    cell_groups: dict[str, dict[str, Any]] = {}
    # edges: list of {from, to, type} where type is 'read'/'write'/'contain'
    edges: list[dict[str, Any]] = []

    def group_id(reader_rid: str, writer_rid: str) -> str:
        return f"{reader_rid}|{writer_rid}"

    # BFS from root; each RID processed once (DAG, not tree)
    queue: list[str] = [result.root_rid]
    seen: set[str] = {result.root_rid}

    def ensure_request(rid: str) -> dict[str, Any]:
        if rid not in requests:
            requests[rid] = {
                "id": rid,
                "name": rid,
                "rid": rid,
                "nodeType": "request",
                "isRoot": rid == result.root_rid,
            }
        return requests[rid]

    ensure_request(result.root_rid)

    while queue:
        rid = queue.pop(0)
        pairs: dict[str, list[Any]] = {}
        for link in links_by_child.get(rid, []):
            pairs.setdefault(link.parent_rid, []).append(link)

        for parent_rid, group_links in pairs.items():
            gid = group_id(rid, parent_rid)
            if gid not in cell_groups:
                if len(group_links) == 1:
                    cell_node = _cell_data(group_links[0])
                    cell_node["id"] = gid
                    cell_node["readerRid"] = rid
                    cell_node["writerRid"] = parent_rid
                    cell_groups[gid] = cell_node
                else:
                    cell_groups[gid] = {
                        "id": gid,
                        "name": f"{len(group_links)} cells",
                        "nodeType": "cell-group",
                        "cellCount": len(group_links),
                        "readerRid": rid,
                        "writerRid": parent_rid,
                        "cells": [_cell_data(l) for l in group_links],
                        "certainty": (
                            "uncertain"
                            if any(l.certainty == "uncertain" for l in group_links)
                            else "certain"
                        ),
                    }
            edges.append({"from": gid, "to": rid, "type": "read"})
            edges.append({"from": parent_rid, "to": gid, "type": "write"})
            if parent_rid not in seen:
                seen.add(parent_rid)
                ensure_request(parent_rid)
                queue.append(parent_rid)

    return {
        "requests": list(requests.values()),
        "cellGroups": list(cell_groups.values()),
        "edges": edges,
        "rootRid": result.root_rid,
    }


def write_dot(result: TraceResult, path: str | Path) -> None:
    """Render the cell provenance graph as an interactive light-theme HTML page.

    The original .dot output was hard to read; this produces an ECharts-based
    tree visualization with a detail sidebar. The output path keeps its name
    (often ``.dot`` for CLI compatibility) but the content is HTML.
    """
    tree_data = _build_cell_tree(result)
    tree_json = (
        json.dumps(tree_data, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )

    n_requests = len(result.graph_request_ids)
    n_links = len(result.links)
    n_uncertain = sum(link.certainty == "uncertain" for link in result.links)
    distinct_cells = {link.cell for link in result.links}
    n_cells = len(distinct_cells)
    n_tables = len({cell[0] for cell in distinct_cells})
    n_rows = len({cell[:2] for cell in distinct_cells})
    mode_label = result.mode.upper()

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Cell-Level Attack Provenance - {result.root_rid}</title>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
  <style>
    :root {{
      --bg: #f6f8fb;
      --panel: #ffffff;
      --panel-2: #eef2f7;
      --border: #d8e0ea;
      --text: #1e293b;
      --muted: #64748b;
      --accent-blue: #2563eb;
      --accent-red: #dc2626;
      --accent-green: #059669;
      --accent-amber: #d97706;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; padding: 0;
      background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      display: flex; height: 100vh; overflow: hidden;
    }}
    #sidebar {{
      width: 420px; background: var(--panel);
      border-right: 1px solid var(--border);
      display: flex; flex-direction: column;
      box-shadow: 2px 0 12px rgba(15,23,42,0.06);
    }}
    #main {{
      flex: 1; position: relative;
      background: radial-gradient(circle at center, #ffffff 0%, #eef2f7 100%);
    }}
    #chart {{ width: 100%; height: 100%; }}

    .header {{ padding: 22px 20px; border-bottom: 1px solid var(--border); background: var(--panel); }}
    .header h1 {{ margin: 0 0 4px; font-size: 20px; color: #0f172a; letter-spacing: 0.3px; }}
    .header .sub {{ font-size: 12px; color: var(--muted); margin-bottom: 14px; font-family: ui-monospace, monospace; }}
    .stats {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 6px; }}
    .stat-box {{ background: var(--panel-2); padding: 10px 4px; border-radius: 8px; text-align: center; border: 1px solid var(--border); }}
    .stat-value {{ font-size: 19px; font-weight: bold; color: var(--accent-blue); }}
    .stat-label {{ font-size: 11px; color: var(--muted); margin-top: 3px; }}

    .legend {{ padding: 10px 20px; border-bottom: 1px solid var(--border); display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--muted); }}
    .legend-item {{ display: inline-flex; align-items: center; gap: 6px; }}
    .legend-swatch {{ width: 18px; height: 3px; border-radius: 2px; }}
    .legend-swatch.read {{ background: var(--accent-blue); }}
    .legend-swatch.write {{ background: var(--accent-red); }}
    .legend-swatch.uncertain {{ border-top: 3px dashed var(--accent-amber); height: 0; }}
    .legend-dir {{ color: var(--muted); font-size: 11px; margin-top: 6px; }}

    #detail-panel {{ flex: 1; overflow-y: auto; padding: 20px; }}
    .detail-placeholder {{ color: #94a3b8; text-align: center; margin-top: 60px; font-size: 14px; display: flex; flex-direction: column; align-items: center; gap: 12px; }}
    .detail-placeholder svg {{ width: 44px; height: 44px; opacity: 0.5; }}

    .detail-card {{ display: none; animation: fadeIn 0.25s ease; }}
    .detail-card.active {{ display: block; }}
    @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(4px); }} to {{ opacity: 1; transform: translateY(0); }} }}

    .tag {{ display: inline-flex; align-items: center; padding: 5px 11px; border-radius: 6px; font-size: 12px; font-weight: bold; margin-bottom: 18px; letter-spacing: 0.4px; }}
    .tag.request {{ background: rgba(37,99,235,0.10); color: var(--accent-blue); border: 1px solid rgba(37,99,235,0.25); }}
    .tag.root {{ background: rgba(220,38,38,0.10); color: var(--accent-red); border: 1px solid rgba(220,38,38,0.25); }}
    .tag.cell {{ background: rgba(5,150,105,0.10); color: var(--accent-green); border: 1px solid rgba(5,150,105,0.25); }}

    .prop-group {{ margin-bottom: 16px; }}
    .prop-label {{ font-size: 11px; color: var(--muted); margin-bottom: 5px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }}
    .prop-value {{ font-size: 13px; background: var(--panel-2); padding: 10px; border-radius: 8px; border: 1px solid var(--border); word-break: break-all; line-height: 1.5; color: var(--text); }}
    pre {{ margin: 0; white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; color: #0f766e; }}

    .taint-tag {{ display: inline-block; padding: 3px 10px; border-radius: 5px; font-size: 12px; font-weight: bold; }}
    .taint-true {{ background: rgba(220,38,38,0.12); color: var(--accent-red); border: 1px solid rgba(220,38,38,0.25); }}
    .taint-false {{ background: rgba(100,116,139,0.12); color: var(--muted); border: 1px solid rgba(100,116,139,0.25); }}

    .tag.group {{ background: rgba(217,119,6,0.10); color: var(--accent-amber); border: 1px solid rgba(217,119,6,0.25); }}
    .cell-list-item {{ display: flex; justify-content: space-between; align-items: center; padding: 8px 10px; margin-bottom: 5px; background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px; cursor: pointer; transition: background 0.15s; }}
    .cell-list-item:hover {{ background: #e0e7ef; }}
    .cell-list-name {{ font-size: 12px; font-family: ui-monospace, monospace; color: #064e3b; font-weight: 600; }}
    .cell-list-val {{ font-size: 11px; color: var(--muted); font-family: ui-monospace, monospace; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  </style>
</head>
<body>
  <div id="sidebar">
    <div class="header">
      <h1>Cell-Level Attack Provenance</h1>
      <div class="sub">root_rid: {result.root_rid} - mode: {mode_label}</div>
      <div class="stats">
        <div class="stat-box"><div class="stat-value">{n_requests}</div><div class="stat-label">Requests</div></div>
        <div class="stat-box"><div class="stat-value">{n_cells}</div><div class="stat-label">Cells</div></div>
        <div class="stat-box"><div class="stat-value">{n_tables}</div><div class="stat-label">Tables</div></div>
        <div class="stat-box"><div class="stat-value">{n_rows}</div><div class="stat-label">Rows</div></div>
        <div class="stat-box"><div class="stat-value">{n_links}</div><div class="stat-label">Links - Uncertain {n_uncertain}</div></div>
      </div>
    </div>
    <div class="legend">
      <span class="legend-item"><span class="legend-swatch write"></span>WRITE (request -> cell)</span>
      <span class="legend-item"><span class="legend-swatch read"></span>READ (cell -> request)</span>
      <span class="legend-item"><span class="legend-swatch uncertain"></span>UNCERTAIN (not recursive)</span>
      <span class="legend-item" style="color:var(--accent-amber);">Cell group (click for details)</span>
      <div class="legend-dir">Upward provenance: the attack root request is at the bottom; predecessor writers are traced upward</div>
    </div>
    <div id="detail-panel">
      <div class="detail-placeholder" id="placeholder">
        <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M15 15l-2 5L9 9l11 4-5 2zm0 0l5 5M7.188 2.239l.777 2.897M5.136 7.965l-2.898-.777M13.95 4.05l-2.122 2.122m-5.657 5.656l-2.12 2.122"></path></svg>
        <span>Click a graph node to inspect details</span>
      </div>

      <div class="detail-card" id="card-request">
        <div class="tag request" id="req-tag">Request Node</div>
        <div class="prop-group"><div class="prop-label">Request ID</div><div class="prop-value" id="req-id"></div></div>
      </div>

      <div class="detail-card" id="card-cell">
        <div class="tag cell">Cell Node</div>
        <div class="prop-group"><div class="prop-label">Cell</div><div class="prop-value" id="cell-name" style="font-weight:600;color:#0f172a;"></div></div>
        <div class="prop-group"><div class="prop-label">Value</div><div class="prop-value"><pre id="cell-value"></pre></div></div>
        <div class="prop-group"><div class="prop-label">Origin Write</div><div class="prop-value"><span id="cell-op"></span> by <span id="cell-origin" style="font-weight:bold;color:var(--accent-blue);"></span><br/><span id="cell-write-time" style="color:var(--muted);font-size:12px;"></span></div></div>
        <div class="prop-group"><div class="prop-label">Read Time</div><div class="prop-value" id="cell-read-time"></div></div>
        <div class="prop-group"><div class="prop-label">Taint Decision</div><div class="prop-value" id="cell-taint"></div></div>
        <div class="prop-group"><div class="prop-label">Reason</div><div class="prop-value" id="cell-reason"></div></div>
        <div class="prop-group"><div class="prop-label">Evidence</div><div class="prop-value" id="cell-evidence" style="padding:8px;background:transparent;border:none;"></div></div>
      </div>

      <div class="detail-card" id="card-group">
        <div class="tag group">Cell Group</div>
        <div class="prop-group"><div class="prop-label">Group Size</div><div class="prop-value" id="group-count" style="font-weight:600;color:var(--accent-amber);"></div></div>
        <div class="prop-group"><div class="prop-label">Writer Request</div><div class="prop-value" id="group-writer" style="font-family:ui-monospace,monospace;"></div></div>
        <div class="prop-group"><div class="prop-label">Included Cells (click for details)</div><div id="group-cell-list"></div></div>
      </div>
    </div>
  </div>

  <div id="main"><div id="chart"></div></div>

  <script>
    const dag = {tree_json};
    const chartDom = document.getElementById('chart');

    if (typeof echarts === 'undefined') {{
      chartDom.innerHTML = '<div style="color:#dc2626; padding: 40px; text-align: center; font-size: 16px;">Failed to load ECharts. Check network access to cdn.jsdelivr.net.</div>';
    }} else {{
      const myChart = echarts.init(chartDom, null, {{ renderer: 'canvas' }});

      // Index the DAG
      const reqMap = new Map(); // rid -> request node
      dag.requests.forEach(r => reqMap.set(r.id, r));
      const groupMap = new Map(); // gid -> group/cell node
      dag.cellGroups.forEach(g => groupMap.set(g.id, g));

      function styleForNode(n) {{
        if (n.nodeType === 'request') {{
          const isRoot = !!n.isRoot;
          return {{
            symbol: 'roundRect', symbolSize: [26, 26],
            itemStyle: {{ color: isRoot ? '#fee2e2' : '#dbeafe', borderColor: isRoot ? '#dc2626' : '#2563eb', borderWidth: 2 }},
            label: {{ show: true, position: 'top', distance: 10, fontSize: 13, fontWeight: 'bold', color: '#0f172a',
              backgroundColor: isRoot ? '#fecaca' : '#bfdbfe', padding: [5,9], borderRadius: 6, borderWidth: 1,
              borderColor: isRoot ? '#dc2626' : '#2563eb',
              formatter: () => n.rid.length > 16 ? n.rid.slice(0,8) + '...' : n.rid }},
          }};
        }}
        if (n.nodeType === 'cell-group') {{
          return {{
            symbol: 'diamond', symbolSize: 36,
            itemStyle: {{ color: '#fef3c7', borderColor: '#d97706', borderWidth: 2 }},
            label: {{ show: true, position: 'bottom', distance: 10, fontSize: 13, fontWeight: 'bold', color: '#92400e',
              backgroundColor: '#fde68a', padding: [4,10], borderRadius: 5, borderWidth: 1, borderColor: '#d97706',
              formatter: () => n.cellCount + ' cells' }},
          }};
        }}
        if (n.nodeType === 'cell') {{
          return {{
            symbol: 'diamond', symbolSize: 22,
            itemStyle: {{ color: '#d1fae5', borderColor: '#059669', borderWidth: 1.5 }},
            label: {{ show: true, position: 'bottom', distance: 8, fontSize: 11, color: '#064e3b',
              backgroundColor: '#a7f3d0', padding: [2,5], borderRadius: 3, borderWidth: 1, borderColor: '#059669',
              formatter: () => n.column || n.name }},
          }};
        }}
        return {{
          symbol: 'circle', symbolSize: 12,
          itemStyle: {{ color: '#cbd5e1', borderColor: '#64748b' }},
          label: {{ show: true, position: 'right', color: '#64748b', fontStyle: 'italic', formatter: () => n.name }},
        }};
      }}

      function render() {{
        const nodes = [], links = [];
        const X_STEP = 210;
        const Y_STEP = 150;

        /*
         * Sugiyama-style layered layout for the provenance DAG.
         *
         * Treat reader -> cell-group -> writer as the backtrace direction.
         * Longest-path ranks ensure every writer is above every reader even
         * when one request is shared by several branches. Barycentric sweeps
         * then order each rank to reduce crossings. This avoids the previous
         * DFS behaviour where shared request nodes inherited the same x value.
         */
        const layoutNodes = new Map();
        reqMap.forEach((r, id) => layoutNodes.set(id, r));
        groupMap.forEach((g, id) => layoutNodes.set(id, g));

        const backtraceEdges = [];
        dag.cellGroups.forEach(g => {{
          backtraceEdges.push([g.readerRid, g.id]);
          // A self-write remains visible as a request -> cell -> same-request
          // loop, but must not increase the request's longest-path rank.
          if (g.writerRid !== g.readerRid) {{
            backtraceEdges.push([g.id, g.writerRid]);
          }}
        }});

        const rank = new Map([[dag.rootRid, 0]]);
        for (let pass = 0; pass < layoutNodes.size; pass += 1) {{
          let changed = false;
          backtraceEdges.forEach(([from, to]) => {{
            if (!rank.has(from)) return;
            const candidate = rank.get(from) + 1;
            if (!rank.has(to) || candidate > rank.get(to)) {{
              rank.set(to, candidate);
              changed = true;
            }}
          }});
          if (!changed) break;
        }}

        // Defensive fallback for malformed/cyclic input: keep every node visible.
        layoutNodes.forEach((_, id) => {{
          if (!rank.has(id)) rank.set(id, 0);
        }});
        const maxRank = Math.max(0, ...rank.values());
        const layers = Array.from({{ length: maxRank + 1 }}, () => []);
        layoutNodes.forEach((node, id) => layers[rank.get(id)].push(id));
        layers.forEach(layer => layer.sort((a, b) => a.localeCompare(b)));

        const neighborsTowardRoot = new Map();
        const neighborsTowardPast = new Map();
        backtraceEdges.forEach(([from, to]) => {{
          if (!neighborsTowardPast.has(from)) neighborsTowardPast.set(from, []);
          if (!neighborsTowardRoot.has(to)) neighborsTowardRoot.set(to, []);
          neighborsTowardPast.get(from).push(to);
          neighborsTowardRoot.get(to).push(from);
        }});

        const xOf = new Map();
        function assignLayerSlots(layer) {{
          const offset = (layer.length - 1) / 2;
          layer.forEach((id, index) => xOf.set(id, (index - offset) * X_STEP));
        }}
        layers.forEach(assignLayerSlots);

        function reorderLayer(layer, neighborMap) {{
          const oldIndex = new Map(layer.map((id, index) => [id, index]));
          layer.sort((a, b) => {{
            const barycenter = id => {{
              const adjacent = (neighborMap.get(id) || []).filter(n => xOf.has(n));
              if (!adjacent.length) return xOf.get(id) || 0;
              return adjacent.reduce((sum, n) => sum + xOf.get(n), 0) / adjacent.length;
            }};
            const delta = barycenter(a) - barycenter(b);
            return Math.abs(delta) > 0.001 ? delta : oldIndex.get(a) - oldIndex.get(b);
          }});
          assignLayerSlots(layer);
        }}

        for (let sweep = 0; sweep < 8; sweep += 1) {{
          for (let r = 1; r <= maxRank; r += 1) {{
            reorderLayer(layers[r], neighborsTowardRoot);
          }}
          for (let r = maxRank - 1; r >= 0; r -= 1) {{
            reorderLayer(layers[r], neighborsTowardPast);
          }}
        }}

        const position = new Map();
        layers.forEach((layer, r) => {{
          layer.forEach(id => {{
            position.set(id, {{
              x: xOf.get(id),
              y: (maxRank - r) * Y_STEP,
            }});
          }});
        }});

        reqMap.forEach((r, rid) => {{
          const p = position.get(rid);
          nodes.push(Object.assign(
            {{ id: rid, name: rid, x: p.x, y: p.y, originalNode: r }},
            styleForNode(r)
          ));
        }});

        dag.cellGroups.forEach(g => {{
          const p = position.get(g.id);
          if (!p) return;
          nodes.push(Object.assign(
            {{ id: g.id, name: g.name, x: p.x, y: p.y, originalNode: g }},
            styleForNode(g)
          ));

          links.push({{
            source: g.writerRid,
            target: g.id,
            relation: 'WRITE',
            lineStyle: {{ color: g.certainty === 'uncertain' ? '#d97706' : '#dc2626', width: 2, curveness: 0.06, type: g.certainty === 'uncertain' ? 'dashed' : 'solid' }},
            symbol: ['none', 'arrow'],
            symbolSize: [6, 10],
          }});
          links.push({{
            source: g.id,
            target: g.readerRid,
            relation: g.nodeType === 'cell-group' ? `READ ${{g.cellCount}} cells` : 'READ',
            lineStyle: {{ color: g.certainty === 'uncertain' ? '#d97706' : '#2563eb', width: 2, curveness: 0.06, type: g.certainty === 'uncertain' ? 'dashed' : 'solid' }},
            symbol: ['none', 'arrow'],
            symbolSize: [6, 10],
          }});
        }});

        myChart.setOption({{
          backgroundColor: 'transparent',
          tooltip: {{ trigger: 'item', backgroundColor: '#ffffff', borderColor: '#d8e0ea', padding: 12, borderWidth: 1,
            textStyle: {{ color: '#1e293b' }},
            formatter: function (p) {{
              if (p.dataType === 'edge') return p.data.relation || '';
              const n = p.data.originalNode; if (!n) return p.name;
              if (n.nodeType === 'request') return `<b style="color:${{n.isRoot?'#dc2626':'#2563eb'}}">${{n.isRoot?'Attack Root Request':'Upstream Request'}}</b><br/>${{n.rid}}`;
              if (n.nodeType === 'cell-group') return `<b style="color:#d97706">Cell Group</b><br/>${{n.cellCount}} cells - click for details`;
              if (n.nodeType === 'cell') return `<b style="color:#059669">Cell</b><br/>${{n.name}}`;
              return p.name;
            }}
          }},
          series: [{{ type: 'graph', layout: 'none', roam: true, draggable: false, data: nodes, links: links,
            edgeLabel: {{ show: false }}, emphasis: {{ focus: 'adjacency', lineStyle: {{ width: 3 }} }},
            lineStyle: {{ opacity: 0.82 }}, animationDurationUpdate: 300 }}]
        }}, true);
      }}

      render();

      function showCellDetail(n) {{
        document.getElementById('card-cell').classList.add('active');
        document.getElementById('cell-name').textContent = n.name;
        document.getElementById('cell-value').textContent = typeof n.value === 'string' ? n.value : JSON.stringify(n.value, null, 2);
        document.getElementById('cell-op').textContent = (n.operation || '').toUpperCase();
        document.getElementById('cell-origin').textContent = n.origin_rid;
        document.getElementById('cell-write-time').textContent = n.write_time;
        document.getElementById('cell-read-time').textContent = n.read_time;
        const taintEl = document.getElementById('cell-taint');
        const tainted = n.tainted === true || n.tainted === 'true';
        taintEl.innerHTML = `<span class="taint-tag ${{tainted?'taint-true':'taint-false'}}">${{tainted?'TAINTED':'CLEAN'}}</span> - ${{(n.confidence||'').toUpperCase()}} - ${{n.model||''}}`;
        document.getElementById('cell-reason').textContent = n.reason || '';
        document.getElementById('cell-evidence').innerHTML = (n.evidence && n.evidence.length)
          ? n.evidence.map(e => `<div style="background:var(--panel-2);padding:5px 9px;margin-bottom:5px;border-radius:5px;border:1px solid var(--border);font-family:monospace;font-size:12px;color:#0f766e;">${{e}}</div>`).join('')
          : '<span style="color:#94a3b8">No specific evidence markers</span>';
      }}

      function showGroupDetail(n) {{
        document.getElementById('card-group').classList.add('active');
        document.getElementById('group-count').textContent = n.cellCount + ' cells';
        document.getElementById('group-writer').textContent = n.writerRid;
        const listEl = document.getElementById('group-cell-list');
        listEl.innerHTML = n.cells.map((c, i) =>
          `<div class="cell-list-item" data-idx="${{i}}"><span class="cell-list-name">${{c.name}}</span><span class="cell-list-val">${{(typeof c.value==='string'?c.value:JSON.stringify(c.value)).slice(0,40)}}</span></div>`
        ).join('');
        const cells = n.cells;
        listEl.querySelectorAll('.cell-list-item').forEach(el => {{
          el.addEventListener('click', () => showCellDetail(cells[parseInt(el.dataset.idx)]));
        }});
      }}

      myChart.on('click', function (params) {{
        if (params.dataType !== 'node') return;
        const n = params.data.originalNode; if (!n) return;

        if (n.nodeType === 'cell-group') {{
          document.getElementById('placeholder').style.display = 'none';
          document.querySelectorAll('.detail-card').forEach(c => c.classList.remove('active'));
          showGroupDetail(n);
          return;
        }}

        document.getElementById('placeholder').style.display = 'none';
        document.querySelectorAll('.detail-card').forEach(c => c.classList.remove('active'));

        if (n.nodeType === 'request') {{
          document.getElementById('card-request').classList.add('active');
          const tag = document.getElementById('req-tag');
          tag.className = n.isRoot ? 'tag root' : 'tag request';
          tag.textContent = n.isRoot ? 'Attack Root Request' : 'Upstream Request';
          document.getElementById('req-id').textContent = n.rid;
        }} else if (n.nodeType === 'cell') {{
          showCellDetail(n);
        }}
      }});
      window.addEventListener('resize', () => myChart.resize());
    }}
  </script>
</body>
</html>"""

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_content, encoding="utf-8")
