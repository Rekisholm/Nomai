from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Set, Tuple

from .common import cells_to_row, normalize_table_name, parse_timestamp, stringify_pk
from .models import DataObject, DependencyLink, PrunedRead, ReadEvent, WriteEvent


def format_time_short(value: datetime) -> str:
    return value.strftime("%H:%M:%S")


def shorten_label_text(text: str, limit: int = 28) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "..."


def json_preview(value: Dict[str, Any], limit: int = 220) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def load_read_logs(path: Path, case_sensitive: bool) -> List[ReadEvent]:
    reads: List[ReadEvent] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            record = json.loads(line)
            rid = str(record.get("request_id", record.get("rid", ""))).strip()
            table_name = str(record.get("table_name", record.get("tbn", ""))).strip()
            timestamp = record.get("timestamp", record.get("time"))
            if not rid or not table_name or timestamp is None:
                raise ValueError(f"Read log line {line_number} is missing required fields: {line}")

            pks = record.get("pks")
            if pks is None:
                single_pk = record.get("pk")
                pks = [] if single_pk is None else [single_pk]

            event_time = parse_timestamp(str(timestamp))
            normalized_table = normalize_table_name(table_name, case_sensitive)
            for pk in pks:
                reads.append(ReadEvent(
                    time=event_time,
                    rid=rid,
                    tbn=normalized_table,
                    pk=stringify_pk(pk),
                ))

    reads.sort(key=lambda item: item.time)
    return reads


def load_write_logs(path: Path, case_sensitive: bool) -> List[WriteEvent]:
    writes: List[WriteEvent] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            record = json.loads(line)
            rid = str(record.get("rid", record.get("request_id", ""))).strip()
            table_name = str(record.get("tbn", record.get("table_name", ""))).strip()
            timestamp = record.get("time", record.get("timestamp"))
            pk = record.get("pk")
            event = str(record.get("event", "update"))
            if not rid or not table_name or timestamp is None or pk is None:
                raise ValueError(f"Write log line {line_number} is missing required fields: {line}")

            writes.append(WriteEvent(
                time=parse_timestamp(str(timestamp)),
                rid=rid,
                event=event,
                tbn=normalize_table_name(table_name, case_sensitive),
                pk=stringify_pk(pk),
                row=cells_to_row(record.get("cells", [])),
            ))

    writes.sort(key=lambda item: item.time)
    return writes


def build_indexes(
    reads: Sequence[ReadEvent],
    writes: Sequence[WriteEvent],
) -> Tuple[
    Dict[DataObject, List[WriteEvent]],
    Dict[DataObject, List[datetime]],
    Dict[str, List[ReadEvent]],
]:
    writes_index: Dict[DataObject, List[WriteEvent]] = defaultdict(list)
    for event in writes:
        writes_index[(event.tbn, event.pk)].append(event)

    writes_time_index = {
        data_obj: [item.time for item in items]
        for data_obj, items in writes_index.items()
    }

    request_read_events: Dict[str, List[ReadEvent]] = defaultdict(list)
    for event in reads:
        request_read_events[event.rid].append(event)

    return writes_index, writes_time_index, request_read_events


def compute_paths(root_rid: str, req_parents: Dict[str, Set[str]]) -> List[List[str]]:
    paths: List[List[str]] = []

    def dfs(rid: str, path: List[str], visited: Set[str]) -> None:
        parents = sorted(req_parents.get(rid, []))
        if not parents:
            paths.append(path.copy())
            return
        for parent_rid in parents:
            if parent_rid in visited:
                # Detected a cycle, add the cycle back-edge and stop recursing this branch
                paths.append(path + [f"(cycle: {parent_rid})"])
                continue
            path.append(parent_rid)
            visited.add(parent_rid)
            dfs(parent_rid, path, visited)
            visited.remove(parent_rid)
            path.pop()

    dfs(root_rid, [root_rid], {root_rid})
    return paths


def export_html_report(
    root_rid: str,
    req_parents: Dict[str, Set[str]],
    links: Sequence[DependencyLink],
    pruned_reads: Sequence[PrunedRead],
    output_path: Path,
) -> None:
    links_by_child: Dict[str, List[DependencyLink]] = defaultdict(list)
    for link in sorted(
        links,
        key=lambda item: (min(item.write_event.time, item.read_event.time), item.parent_rid, item.child_rid, item.data_obj),
    ):
        links_by_child[link.child_rid].append(link)

    def build_tree(rid: str, active_path: Set[str]) -> Dict[str, Any]:
        is_root = (rid == root_rid)
        node = {
            "name": rid,
            "rid": rid,
            "nodeType": "request",
            "isRoot": is_root,
            "children": []
        }
        for link in links_by_child.get(rid, []):
            data_name = f"{link.data_obj[0]}[{link.data_obj[1]}]"
            data_node = {
                "name": data_name,
                "nodeType": "data",
                "confidence": link.decision.confidence,
                "reason": link.decision.reason,
                "evidence": link.decision.evidence,
                "write_event": link.write_event.event,
                "write_time": link.write_event.time.isoformat(),
                "read_time": link.read_event.time.isoformat(),
                "row_preview": json.dumps(link.write_event.row, ensure_ascii=False, indent=2, default=str),
                "children": []
            }
            if link.parent_rid in active_path:
                data_node["children"].append({
                    "name": f"Cycle dependency: {link.parent_rid}",
                    "nodeType": "cycle",
                    "rid": link.parent_rid
                })
            else:
                next_path = set(active_path)
                next_path.add(link.parent_rid)
                data_node["children"].append(build_tree(link.parent_rid, next_path))
            node["children"].append(data_node)
        return node

    tree_data = build_tree(root_rid, {root_rid})
    tree_json = json.dumps(tree_data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Attack Request Chain Provenance Analysis</title>
  <script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
  <style>
    :root {{
      --bg: #0f172a;
      --panel: #1e293b;
      --panel-2: #334155;
      --border: #475569;
      --text: #e2e8f0;
      --muted: #94a3b8;
      --accent-blue: #60a5fa;
      --accent-red: #f87171;
      --accent-green: #34d399;
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
      border-right: 1px solid var(--panel-2);
      display: flex; flex-direction: column;
      box-shadow: 2px 0 15px rgba(0,0,0,0.4);
    }}
    #main {{
      flex: 1; position: relative;
      background: radial-gradient(circle at center, #1e293b 0%, #0f172a 100%);
    }}
    #chart {{ width: 100%; height: 100%; }}

    .header {{ padding: 24px 20px; border-bottom: 1px solid var(--panel-2); background: var(--bg); }}
    .header h1 {{ margin: 0 0 16px; font-size: 22px; color: #f8fafc; letter-spacing: 0.5px; }}
    .stats {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    .stat-box {{ background: var(--panel-2); padding: 14px; border-radius: 10px; text-align: center; border: 1px solid var(--border); }}
    .stat-value {{ font-size: 24px; font-weight: bold; color: var(--accent-blue); }}
    .stat-label {{ font-size: 13px; color: var(--muted); margin-top: 6px; }}

    .legend {{ padding: 12px 20px; border-bottom: 1px solid var(--panel-2); display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--muted); }}
    .legend-item {{ display: inline-flex; align-items: center; gap: 6px; }}
    .legend-swatch {{ width: 18px; height: 2px; }}
    .legend-swatch.read {{ background: var(--accent-blue); }}
    .legend-swatch.write {{ background: var(--accent-red); }}

    #detail-panel {{ flex: 1; overflow-y: auto; padding: 20px; }}
    .detail-placeholder {{ color: #64748b; text-align: center; margin-top: 60px; font-size: 15px; display: flex; flex-direction: column; align-items: center; gap: 12px; }}
    .detail-placeholder svg {{ width: 48px; height: 48px; opacity: 0.5; }}

    .detail-card {{ display: none; animation: fadeIn 0.3s ease; }}
    .detail-card.active {{ display: block; }}
    @keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(5px); }} to {{ opacity: 1; transform: translateY(0); }} }}

    .tag {{ display: inline-flex; align-items: center; padding: 6px 12px; border-radius: 6px; font-size: 13px; font-weight: bold; margin-bottom: 20px; letter-spacing: 0.5px; }}
    .tag.request {{ background: rgba(59, 130, 246, 0.15); color: var(--accent-blue); border: 1px solid rgba(59, 130, 246, 0.3); }}
    .tag.root {{ background: rgba(239, 68, 68, 0.15); color: var(--accent-red); border: 1px solid rgba(239, 68, 68, 0.3); }}
    .tag.data {{ background: rgba(16, 185, 129, 0.15); color: var(--accent-green); border: 1px solid rgba(16, 185, 129, 0.3); }}

    .prop-group {{ margin-bottom: 20px; }}
    .prop-label {{ font-size: 12px; color: var(--muted); margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }}
    .prop-value {{ font-size: 14px; background: var(--bg); padding: 12px; border-radius: 8px; border: 1px solid var(--panel-2); word-break: break-all; line-height: 1.5; color: var(--text); }}
    pre {{ margin: 0; white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 13px; color: #a7f3d0; }}

    .evidence-item {{ background: var(--panel); padding: 6px 10px; margin-bottom: 6px; border-radius: 6px; border: 1px solid var(--panel-2); font-family: monospace; font-size: 13px; color: #bae6fd; }}
    .evidence-item:last-child {{ margin-bottom: 0; }}

    .confidence-high {{ color: #ef4444; font-weight: bold; border-color: rgba(239, 68, 68, 0.3) !important; background: rgba(239, 68, 68, 0.05) !important; }}
    .confidence-medium {{ color: #f59e0b; font-weight: bold; border-color: rgba(245, 158, 11, 0.3) !important; background: rgba(245, 158, 11, 0.05) !important; }}
    .confidence-low {{ color: #10b981; font-weight: bold; border-color: rgba(16, 185, 129, 0.3) !important; background: rgba(16, 185, 129, 0.05) !important; }}
  </style>
</head>
<body>
  <div id="sidebar">
    <div class="header">
      <h1>Attack Request Chain Provenance Analysis</h1>
      <div class="stats">
        <div class="stat-box">
          <div class="stat-value">{len(links)}</div>
          <div class="stat-label">Tainted Dependencies Kept</div>
        </div>
        <div class="stat-box">
          <div class="stat-value">{len(pruned_reads)}</div>
          <div class="stat-label">Benign Read Dependencies Pruned</div>
        </div>
      </div>
    </div>
    <div class="legend">
      <span class="legend-item"><span class="legend-swatch read"></span>Read (data -> request)</span>
      <span class="legend-item"><span class="legend-swatch write"></span>Write (request -> data)</span>
    </div>
    <div id="detail-panel">
      <div class="detail-placeholder" id="placeholder">
        <svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M15 15l-2 5L9 9l11 4-5 2zm0 0l5 5M7.188 2.239l.777 2.897M5.136 7.965l-2.898-.777M13.95 4.05l-2.122 2.122m-5.657 5.656l-2.12 2.122"></path></svg>
        <span>Click a graph node to view details</span>
      </div>

      <div class="detail-card" id="card-request">
        <div class="tag request" id="req-tag">Request Node</div>
        <div class="prop-group">
          <div class="prop-label">Request ID</div>
          <div class="prop-value" id="req-id"></div>
        </div>
      </div>

      <div class="detail-card" id="card-data">
        <div class="tag data">Data Node</div>
        <div class="prop-group">
          <div class="prop-label">Table & PK</div>
          <div class="prop-value" id="data-name" style="font-weight: 600; color: #fff;"></div>
        </div>
        <div class="prop-group">
          <div class="prop-label">Write Event</div>
          <div class="prop-value"><span id="data-write-event" style="font-weight: bold; color: var(--accent-blue);"></span> at <span id="data-write-time"></span></div>
        </div>
        <div class="prop-group">
          <div class="prop-label">Read Time</div>
          <div class="prop-value" id="data-read-time"></div>
        </div>
        <div class="prop-group">
          <div class="prop-label">LLM Taint Confidence</div>
          <div class="prop-value" id="data-confidence"></div>
        </div>
        <div class="prop-group">
          <div class="prop-label">Decision Reason</div>
          <div class="prop-value" id="data-reason"></div>
        </div>
        <div class="prop-group">
          <div class="prop-label">Suspicious Evidence</div>
          <div class="prop-value" id="data-evidence" style="padding: 8px; background: transparent; border: none;"></div>
        </div>
        <div class="prop-group">
          <div class="prop-label">Row Data (After Write)</div>
          <div class="prop-value"><pre id="data-row"></pre></div>
        </div>
      </div>
    </div>
  </div>

  <div id="main">
    <div id="chart"></div>
  </div>

  <script>
    const treeData = {tree_json};
    const chartDom = document.getElementById('chart');

    if (typeof echarts === 'undefined') {{
      chartDom.innerHTML = '<div style="color:#ef4444; padding: 40px; text-align: center; font-size: 18px;">Failed to load ECharts. Check network access to cdn.jsdelivr.net.</div>';
    }} else {{
      // ----- Layout: assign hierarchical (x, y) to each tree node, then flatten into graph nodes/links.
      // Root is placed at x=0; deeper ancestors are pushed leftward (negative x).
      // Edge direction encodes data flow:
      //   read  : data -> request (consumer pulls data)
      //   write : request -> data (producer emits data)
      // All arrows therefore point rightward toward the attack root request.
      const X_STEP = 240;
      const Y_STEP = 90;

      const nodes = [];
      const links = [];
      let leafCursor = 0;

      function assignLayout(node, depth) {{
        node._depth = depth;
        if (!node.children || node.children.length === 0) {{
          node._y = leafCursor * Y_STEP;
          leafCursor += 1;
        }} else {{
          node.children.forEach(child => assignLayout(child, depth + 1));
          const ys = node.children.map(c => c._y);
          node._y = (Math.min(...ys) + Math.max(...ys)) / 2;
        }}
        node._x = -depth * X_STEP;
      }}
      assignLayout(treeData, 0);

      function styleForNode(node) {{
        if (node.nodeType === 'request') {{
          const isRoot = !!node.isRoot;
          return {{
            symbol: 'roundRect',
            symbolSize: [22, 22],
            itemStyle: {{
              color: isRoot ? '#ef4444' : '#3b82f6',
              borderColor: isRoot ? '#f87171' : '#60a5fa',
              borderWidth: 2,
            }},
            label: {{
              show: true, position: 'top', distance: 10,
              fontSize: 14, fontWeight: 'bold', color: '#f8fafc',
              backgroundColor: isRoot ? '#ef444440' : '#3b82f640',
              padding: [6, 10], borderRadius: 6, borderWidth: 1,
              borderColor: isRoot ? '#ef4444' : '#3b82f6',
              formatter: () => node.rid,
            }},
          }};
        }}
        if (node.nodeType === 'data') {{
          return {{
            symbol: 'diamond',
            symbolSize: 26,
            itemStyle: {{ color: '#10b981', borderColor: '#34d399', borderWidth: 2 }},
            label: {{
              show: true, position: 'bottom', distance: 10,
              fontSize: 13, color: '#a7f3d0',
              backgroundColor: '#10b98120',
              padding: [4, 8], borderRadius: 4, borderWidth: 1,
              borderColor: '#10b98160',
              formatter: () => node.name,
            }},
          }};
        }}
        // cycle marker
        return {{
          symbol: 'circle',
          symbolSize: 12,
          itemStyle: {{ color: '#64748b', borderColor: '#94a3b8' }},
          label: {{ show: true, position: 'right', color: '#94a3b8', fontStyle: 'italic', formatter: () => node.name }},
        }};
      }}

      function makeLink(parent, child) {{
        // parent is shallower (closer to root request), child is deeper (ancestor in causality).
        // request -> data child  : the request reads data => semantic edge data -> request (READ)
        // data    -> request grandchild : the grandchild request writes data => semantic edge grandchild -> data (WRITE)
        const isRead = parent.nodeType === 'request' && child.nodeType === 'data';
        const isWrite = parent.nodeType === 'data' && child.nodeType === 'request';

        if (isRead) {{
          return {{
            source: child._gid, target: parent._gid,
            label: {{
              show: true, formatter: 'READ', color: '#bfdbfe',
              backgroundColor: '#1e3a8a', padding: [2, 6], borderRadius: 4, fontSize: 11,
            }},
            lineStyle: {{ color: '#60a5fa', width: 2, curveness: 0 }},
            symbol: ['none', 'arrow'], symbolSize: [6, 10],
          }};
        }}
        if (isWrite) {{
          const evt = (parent.write_event || 'write').toUpperCase();
          return {{
            source: child._gid, target: parent._gid,
            label: {{
              show: true, formatter: `WRITE:${{evt}}`, color: '#fecaca',
              backgroundColor: '#7f1d1d', padding: [2, 6], borderRadius: 4, fontSize: 11,
            }},
            lineStyle: {{ color: '#f87171', width: 2, curveness: 0 }},
            symbol: ['none', 'arrow'], symbolSize: [6, 10],
          }};
        }}
        // cycle / fallback edge (no semantic direction)
        return {{
          source: parent._gid, target: child._gid,
          lineStyle: {{ color: '#64748b', width: 1, type: 'dashed', curveness: 0 }},
          symbol: ['none', 'arrow'], symbolSize: [5, 8],
        }};
      }}

      function flatten(node, parent) {{
        node._gid = String(nodes.length);
        const style = styleForNode(node);
        nodes.push({{
          id: node._gid,
          name: node.name,
          x: node._x, y: node._y,
          originalNode: node,
          ...style,
        }});
        if (parent) {{
          links.push(makeLink(parent, node));
        }}
        (node.children || []).forEach(c => flatten(c, node));
      }}
      flatten(treeData, null);

      const myChart = echarts.init(chartDom, 'dark', {{ renderer: 'canvas' }});
      const option = {{
        backgroundColor: 'transparent',
        tooltip: {{
          trigger: 'item',
          backgroundColor: '#1e293b',
          borderColor: '#475569',
          padding: 12,
          textStyle: {{ color: '#e2e8f0' }},
          formatter: function (params) {{
            if (params.dataType === 'edge') {{
              const lbl = (params.data.label && params.data.label.formatter) || '';
              return `<div style="font-weight:bold;margin-bottom:4px;">Edge</div>${{lbl}}`;
            }}
            const n = params.data.originalNode;
            if (!n) return params.name;
            if (n.nodeType === 'request') {{
              return `<div style="font-weight:bold;color:${{n.isRoot?'#f87171':'#60a5fa'}};margin-bottom:4px;">${{n.isRoot ? 'Attack Root Request' : 'Upstream Request'}}</div>${{n.rid}}`;
            }} else if (n.nodeType === 'data') {{
              return `<div style="font-weight:bold;color:#34d399;margin-bottom:4px;">Data Flow</div>${{n.name}}<br/><span style="color:#94a3b8;font-size:12px;">Click for details</span>`;
            }}
            return params.name;
          }}
        }},
        series: [{{
          type: 'graph',
          layout: 'none',
          roam: true,
          draggable: true,
          data: nodes,
          links: links,
          edgeLabel: {{ show: true }},
          emphasis: {{ focus: 'adjacency', lineStyle: {{ width: 3 }} }},
          lineStyle: {{ opacity: 0.9 }},
          animationDurationUpdate: 400,
        }}]
      }};
      myChart.setOption(option);

      myChart.on('click', function (params) {{
        if (params.dataType !== 'node') return;
        const n = params.data.originalNode;
        if (!n) return;

        document.getElementById('placeholder').style.display = 'none';
        document.getElementById('card-request').classList.remove('active');
        document.getElementById('card-data').classList.remove('active');

        if (n.nodeType === 'request') {{
          document.getElementById('card-request').classList.add('active');
          const tag = document.getElementById('req-tag');
          if (n.isRoot) {{
            tag.className = 'tag root';
            tag.textContent = 'Attack Root Request';
          }} else {{
            tag.className = 'tag request';
            tag.textContent = 'Upstream Request';
          }}
          document.getElementById('req-id').textContent = n.rid;
        }} else if (n.nodeType === 'data') {{
          document.getElementById('card-data').classList.add('active');
          document.getElementById('data-name').textContent = n.name;
          document.getElementById('data-write-event').textContent = (n.write_event || '').toUpperCase();
          document.getElementById('data-write-time').textContent = n.write_time;
          document.getElementById('data-read-time').textContent = n.read_time;

          const confEl = document.getElementById('data-confidence');
          confEl.textContent = (n.confidence || '').toUpperCase();
          confEl.className = 'prop-value confidence-' + (n.confidence || '').toLowerCase();

          document.getElementById('data-reason').textContent = n.reason;
          document.getElementById('data-evidence').innerHTML = n.evidence && n.evidence.length
            ? n.evidence.map(e => `<div class="evidence-item">${{e}}</div>`).join('')
            : '<span style="color:#64748b">No specific evidence markers</span>';

          document.getElementById('data-row').textContent = n.row_preview;
        }}
      }});

      window.addEventListener('resize', () => myChart.resize());
    }}
  </script>
</body>
</html>"""
    
    output_path.write_text(html_content, encoding="utf-8")


def write_summary_json(
    *,
    root_rid: str,
    output_path: Path,
    req_parents: Dict[str, Set[str]],
    links: Sequence[DependencyLink],
    pruned_reads: Sequence[PrunedRead],
    decision_audit: Sequence[Dict[str, Any]],
) -> None:
    paths = compute_paths(root_rid, req_parents)
    predecessor_requests = sorted(
        {rid for path in paths for rid in path if rid != root_rid}
        | {link.parent_rid for link in links if link.parent_rid != root_rid}
    )

    summary = {
        "root_rid": root_rid,
        "predecessor_requests": predecessor_requests,
        "paths": paths,
        "dependencies": [
            {
                "parent_rid": link.parent_rid,
                "child_rid": link.child_rid,
                "table_name": link.data_obj[0],
                "pk": link.data_obj[1],
                "write_event": {
                    "time": link.write_event.time.isoformat(),
                    "event": link.write_event.event,
                    "row": link.write_event.row,
                },
                "read_event": {"time": link.read_event.time.isoformat()},
                "decision": {
                    "tainted": link.decision.is_tainted,
                    "confidence": link.decision.confidence,
                    "reason": link.decision.reason,
                    "evidence": link.decision.evidence,
                    "model": link.decision.model,
                },
            }
            for link in links
        ],
        "pruned_reads": [
            {
                "rid": item.rid,
                "table_name": item.data_obj[0],
                "pk": item.data_obj[1],
                "read_time": item.read_event.time.isoformat(),
                "checked_writers": item.checked_writers,
            }
            for item in pruned_reads
        ],
        "decision_audit": list(decision_audit),
    }
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
