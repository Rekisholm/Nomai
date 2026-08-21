#!/usr/bin/env python3
import argparse
import bisect
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

try:
    from .pruning_algorithm import load_access_logs
except ImportError:
    from pruning_algorithm import load_access_logs


@dataclass(frozen=True)
class Policy:
    block_tables: frozenset
    block_rows: frozenset
    gray_tables: frozenset
    gray_rows: frozenset

    def decide(self, data):
        if (data in self.block_rows or data[0] in self.block_tables or
                data in self.gray_rows or data[0] in self.gray_tables):
            return "gray"
        return "keep"


@dataclass(frozen=True)
class TraceLink:
    parent_rid: str
    data_object: tuple
    child_rid: str
    write: object
    read: object
    decision: str
    recurse: bool


@dataclass
class TraceResult:
    root_rid: str
    links: list
    visited_requests: set
    suppressed_by_reason: dict
    suppressed_examples: list
    raw_request_count: int = 0
    raw_data_count: int = 0

    @property
    def request_ids(self):
        result = {self.root_rid}
        for x in self.links:
            result.update((x.parent_rid, x.child_rid))
        return result


def load_policy(path):
    x = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = []
    for row in x.get("block_rows", []):
        if isinstance(row, dict):
            rows.append((str(row["table"]).lower(), str(row["pk"])))
        else:
            table, pk = str(row)[:-1].split("[", 1)
            rows.append((table.lower(), pk))
    gray_rows = []
    for row in x.get("gray_rows", []):
        if isinstance(row, dict):
            gray_rows.append((str(row["table"]).lower(), str(row["pk"])))
        else:
            table, pk = str(row)[:-1].split("[", 1)
            gray_rows.append((table.lower(), pk))
    return Policy(frozenset(t.lower() for t in x.get("block_tables", [])),
                  frozenset(rows), frozenset(t.lower() for t in x.get("gray_tables", [])),
                  frozenset(gray_rows))


def build_indexes(reads, writes):
    writes_by_data, request_writes = defaultdict(list), defaultdict(set)
    for x in writes:
        writes_by_data[x.data_object].append(x)
        request_writes[x.rid].add(x.data_object)
    write_times = {data: [x.time for x in xs] for data, xs in writes_by_data.items()}
    request_reads = defaultdict(dict)
    for x in reads:
        old = request_reads[x.rid].get(x.data_object)
        if old is None or x.time < old.time:
            request_reads[x.rid][x.data_object] = x
    return writes_by_data, write_times, request_reads, request_writes


def find_last_write_before(data, time, writes, times):
    if data not in times:
        return None
    i = bisect.bisect_right(times[data], time) - 1
    return writes[data][i] if i >= 0 else None


def must_keep(root, child, parent, candidates, request_writes, policy, current_data=None):
    """Decide whether to recurse through a gray dependency.

    Blocked dependencies are downgraded to gray: noisy rows are kept for one hop
    but are not recursively expanded.
    Direct dependencies of the root are the exception because the first step from
    the attack trigger must be expanded.
    """
    if child == root:
        return True
    return False


def build_pruned_trace(root, reads, writes, policy, seed_threshold=30):
    writes_by_data, times, request_reads, request_writes = build_indexes(reads, writes)
    seed_writers = {rid for rid, objs in request_writes.items() if len(objs) >= seed_threshold}
    queue, visited, links = deque([str(root)]), set(), []
    suppressed, examples = defaultdict(int), []
    while queue:
        child = queue.popleft()
        if child in visited:
            continue
        visited.add(child)
        candidates = []
        for data, read in request_reads.get(child, {}).items():
            decision = policy.decide(data)
            write = find_last_write_before(data, read.time, writes_by_data, times)
            if write and write.rid != child:
                if write.rid in seed_writers:
                    suppressed["seed-writer"] += 1
                    if len(examples) < 20:
                        examples.append((child, data, "seed-writer"))
                    continue
                candidates.append((data, write, read, decision))
        for data, write, read, decision in candidates:
            recurse = decision == "keep" or must_keep(root, child, write.rid, candidates,
                                                       request_writes, policy, data)
            links.append(TraceLink(write.rid, data, child, write, read, decision, recurse))
            if recurse and write.rid not in visited:
                queue.append(write.rid)
    return TraceResult(str(root), links, visited, dict(suppressed), examples)


def raw_size(root, reads, writes):
    empty = Policy(frozenset(), frozenset(), frozenset(), frozenset())
    result = build_pruned_trace(root, reads, writes, empty)
    return len(result.request_ids), len(result.links)


def esc(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def export_dot(result, path):
    request_times = {}
    for x in result.links:
        for rid, time in ((x.parent_rid, x.write.time), (x.child_rid, x.read.time)):
            request_times[rid] = min(time, request_times.get(rid, time))
    blocked = sum(result.suppressed_by_reason.values())
    context = sum(x.decision == "gray" and not x.recurse for x in result.links)
    lines = ["digraph pruned_attack_backtrace {", '  rankdir=TB; graph [ordering="out", labelloc="t"];',
             '  node [style="filled", fontsize=10];',
             f'  label="Pruned provenance: {blocked} blocked dependencies; {context} context-only";']
    for rid in sorted(result.request_ids):
        label = f"R:{rid}" + (f"\n{request_times[rid].isoformat()}" if rid in request_times else "")
        if rid == result.root_rid:
            attrs = 'shape=doubleoctagon, fillcolor="#ffcc80"'
        else:
            attrs = 'shape=box, fillcolor="#d9edf7"'
        lines.append(f'  "R:{esc(rid)}" [label="{esc(label)}", {attrs}];')
    for i, x in enumerate(result.links):
        table, pk = x.data_object
        node = f"D:{table}[{pk}]#{i}"
        gray = x.decision == "gray" and not x.recurse
        style = ', color="#888888", fontcolor="#666666"' if gray else ""
        edge = ', style="dotted", color="#888888"' if gray else ""
        fill = "#eeeeee" if gray else "#dff0d8"
        lines += [f'  "{esc(node)}" [label="D:{esc(table)}[{esc(pk)}]", shape=ellipse, fillcolor="{fill}"{style}];',
                  f'  "R:{esc(x.parent_rid)}" -> "{esc(node)}" [label="WRITE"{edge}];',
                  f'  "{esc(node)}" -> "R:{esc(x.child_rid)}" [label="READ"{edge}];']
    if blocked:
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(result.suppressed_by_reason.items()))
        lines.append(f'  "PRUNED" [label="suppressed {blocked} dependencies\\n{reasons}", '
                     'shape=note, fillcolor="#f5f5f5", color="#999999"];')
    lines.append("}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_stats(result, path):
    raw = result.raw_request_count + result.raw_data_count
    pruned = len(result.request_ids) + len(result.links)
    stats = {
        "root_rid": result.root_rid,
        "raw": {"request_nodes": result.raw_request_count, "data_nodes": result.raw_data_count,
                "total_nodes": raw},
        "pruned": {"request_nodes": len(result.request_ids), "data_nodes": len(result.links),
                   "total_nodes": pruned},
        "graph_reduction": 1 - pruned / raw if raw else 0.0,
        "suppressed_dependencies": result.suppressed_by_reason,
        "context_only_dependencies": sum(x.decision == "gray" and not x.recurse for x in result.links),
        "suppressed_examples": [{"child_rid": rid, "table": data[0], "pk": data[1], "reason": reason}
                                for rid, data, reason in result.suppressed_examples],
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-log", required=True)
    parser.add_argument("--write-log", required=True)
    parser.add_argument("--root-rid", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--output", default="causal_tree_with_data_pruned.dot")
    parser.add_argument("--stats-output")
    parser.add_argument("--skip-baseline", action="store_true")
    args = parser.parse_args()
    reads, writes = load_access_logs(args.read_log, "read"), load_access_logs(args.write_log, "write")
    result = build_pruned_trace(args.root_rid, reads, writes, load_policy(args.policy))
    if not args.skip_baseline:
        result.raw_request_count, result.raw_data_count = raw_size(args.root_rid, reads, writes)
    export_dot(result, args.output)
    stats_path = args.stats_output or str(Path(args.output).with_suffix(".stats.json"))
    stats = write_stats(result, stats_path)
    print(f"Pruned Graphviz DOT written to {args.output}")
    print(f"Trace statistics written to {stats_path}")
    print(f"nodes: {stats['raw']['total_nodes']} -> {stats['pruned']['total_nodes']} "
          f"(reduction {stats['graph_reduction']:.1%})")


if __name__ == "__main__":
    main()
