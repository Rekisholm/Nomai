#!/usr/bin/env python3
import argparse
import bisect
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

DataObject = tuple[str, str]


@dataclass(frozen=True)
class RequestRecord:
    rid: str
    time: datetime
    method: str
    path: str

    @property
    def endpoint(self):
        return f"{self.method.upper()} {normalize_path(self.path)}"


@dataclass(frozen=True)
class AccessEvent:
    time: datetime
    rid: str
    table: str
    pk: str
    operation: str

    @property
    def data_object(self):
        return self.table, self.pk


UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
INTEGER = re.compile(r"^-?\d+$")
TOKEN = re.compile(r"^(?:[0-9a-f]{16,}|[A-Za-z0-9_-]{24,})$", re.I)


def parse_time(value):
    value = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(value)


def normalize_path(path):
    parts = []
    for part in (urlsplit(path).path or "/").split("/"):
        if INTEGER.fullmatch(part):
            part = "{id}"
        elif UUID.fullmatch(part):
            part = "{uuid}"
        elif TOKEN.fullmatch(part):
            part = "{token}"
        parts.append(part)
    return "/".join(parts).rstrip("/") or "/"

def normalize_table_name(value):
    table = str(value).strip()
    if "." in table:
        table = table.rsplit(".", 1)[-1]
    return table.lower()

def records(path):
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def load_request_logs(path):
    result = [
        RequestRecord(str(x["rid"]), parse_time(x["time"]), str(x["method"]), str(x["path"]))
        for x in records(path)
    ]
    return sorted(result, key=lambda x: x.time)


def load_access_logs(path, default_operation):
    result = []
    for x in records(path):
        table = normalize_table_name(x.get("table", x.get("tbn")))
        pks = x.get("pks", x.get("pk"))
        pks = pks if isinstance(pks, list) else [pks]
        operation = str(x.get("op", x.get("event", default_operation))).lower()
        result += [
            AccessEvent(parse_time(x["time"]), str(x["rid"]), table, str(pk), operation)
            for pk in pks
        ]
    return sorted(result, key=lambda x: x.time)


def percentile(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    pos = (len(values) - 1) * p
    lo, hi = int(pos), min(int(pos) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def last_write(data, time, writes, times):
    if data not in times:
        return None
    i = bisect.bisect_right(times[data], time) - 1
    return writes[data][i] if i >= 0 else None


def trace_size(root, request_reads, writes, times, depth, blocked=None):
    queue, visited, nodes = deque([(root, 0)]), set(), set()
    while queue:
        rid, level = queue.popleft()
        if rid in visited or level >= depth:
            continue
        visited.add(rid)
        for data, read in request_reads.get(rid, {}).items():
            if data[0] == blocked:
                continue
            write = last_write(data, read.time, writes, times)
            if write and write.rid != rid:
                nodes.update({("r", write.rid), ("d", *data)})
                queue.append((write.rid, level + 1))
    return len(nodes)


def sample_evenly(values, limit):
    if len(values) <= limit:
        return list(values)
    if limit == 1:
        return [values[-1]]
    return [values[round(i * (len(values) - 1) / (limit - 1))] for i in range(limit)]


def build_noise_policy(requests, reads, writes, *, sample_size=128, trace_depth=4):
    if not requests:
        raise ValueError("request log is empty")

    request_ids = {x.rid for x in requests}
    reads = [x for x in reads if x.rid in request_ids]
    endpoints = {x.rid: x.endpoint for x in requests}
    n, m = len(request_ids), len(set(endpoints.values()))

    table_req, table_ep = defaultdict(set), defaultdict(set)
    read_req, write_req = defaultdict(set), defaultdict(set)
    row_req, row_ep, row_writers = defaultdict(set), defaultdict(set), defaultdict(set)
    for x in reads:
        table_req[x.table].add(x.rid)
        table_ep[x.table].add(endpoints[x.rid])
        read_req[x.table].add(x.rid)
        row_req[x.data_object].add(x.rid)
        row_ep[x.data_object].add(endpoints[x.rid])
    for x in writes:
        if x.rid in request_ids:
            table_req[x.table].add(x.rid)
            table_ep[x.table].add(endpoints[x.rid])
            write_req[x.table].add(x.rid)
            row_req[x.data_object].add(x.rid)
            row_ep[x.data_object].add(endpoints[x.rid])
            row_writers[x.data_object].add(x.rid)

    request_reads = defaultdict(dict)
    for x in reads:  # Keep only the earliest read of each row per request.
        old = request_reads[x.rid].get(x.data_object)
        if old is None or x.time < old.time:
            request_reads[x.rid][x.data_object] = x
    writes_by_data = defaultdict(list)
    for x in writes:
        writes_by_data[x.data_object].append(x)
    write_times = {data: [x.time for x in xs] for data, xs in writes_by_data.items()}

    roots = sample_evenly([x.rid for x in requests if x.rid in request_reads], sample_size)
    baseline = [trace_size(r, request_reads, writes_by_data, write_times, trace_depth) for r in roots]
    baseline_mean = sum(baseline) / len(baseline) if baseline else 0.0
    tables = sorted(table_req)
    amp = {}
    for table in tables:
        blocked = [trace_size(r, request_reads, writes_by_data, write_times, trace_depth, table) for r in roots]
        amp[table] = baseline_mean / max(sum(blocked) / len(blocked), 1.0) if blocked else 0.0

    table_metrics = {}
    for table in tables:
        nr, nw = len(read_req[table]), len(write_req[table])
        table_metrics[table] = {
            "request_coverage": len(table_req[table]) / n,
            "endpoint_coverage": len(table_ep[table]) / max(m, 1),
            "rw_balance": min(nr, nw) / max(nr, nw, 1),
            "amplification": amp[table],
            "request_count": len(table_req[table]),
            "endpoint_count": len(table_ep[table]),
            "write_request_count": nw,
            "write_request_coverage": nw / n,
        }
    row_metrics = {
        f"{table}[{pk}]": {
            "table": table, "pk": pk,
            "request_coverage": len(row_req[data]) / n,
            "endpoint_coverage": len(row_ep[data]) / max(m, 1),
            "write_fanout": len(row_writers[data]),
            "request_count": len(row_req[data]),
            "endpoint_count": len(row_ep[data]),
        }
        for data in sorted(row_req) for table, pk in [data]
    }

    # Adaptive thresholds: tc/te use P90 when normal traffic is sufficient,
    # and fall back to conservative lower bounds when traffic is sparse.
    tc = max(percentile([x["request_coverage"] for x in table_metrics.values()], .9), .2)
    te = max(percentile([x["endpoint_coverage"] for x in table_metrics.values()], .9), .3)
    # Fixed constants: rc/am/wf use the same setting across all experiment scenes.
    # re keeps P90 adaptation to cover endpoint diversity differences.
    rc = 0.2
    re_ = max(percentile([x["endpoint_coverage"] for x in row_metrics.values()], .9), .3)
    am = 3.0
    wf = 3.0

    block_tables = []
    for table, x in table_metrics.items():
        x["noise_score"] = sum((min(x["request_coverage"] / tc, 1), min(x["endpoint_coverage"] / te, 1),
                                min(x["amplification"] / am, 1), min(x["rw_balance"] * 2, 1))) / 4
        high_writer_spread = x["write_request_coverage"] >= 0.1
        if (x["request_coverage"] >= tc and x["endpoint_coverage"] >= te
                and x["amplification"] >= am and x["rw_balance"] > 0):
            if not high_writer_spread:
                block_tables.append(table)
            # Tables with high write spread, such as session tables, are not BLOCKed;
            # row-level policies handle them more precisely.

    block_rows = []
    gray_rows = []
    for key, x in row_metrics.items():
        x["noise_score"] = sum((min(x["request_coverage"] / rc, 1), min(x["endpoint_coverage"] / re_, 1))) / 2
        if x["request_coverage"] >= rc and x["endpoint_coverage"] >= re_:
            block_rows.append(key)
        elif x["write_fanout"] >= wf:
            # High-fanout rows, such as session or online-status rows, are written
            # by many requests, making last-writer attribution unreliable.
            # Keep the dependency for context but do not recurse, avoiding pulling
            # all co-writers into the graph.
            gray_rows.append(key)

    return {
        "schema_version": 2, "algorithm": "structural-provenance-noise-v3",
        "block_tables": block_tables, "block_rows": block_rows, "gray_rows": gray_rows,
        "thresholds": {"table_high_coverage": tc, "table_high_endpoint_coverage": te,
                       "row_high_coverage": rc, "row_high_endpoint_coverage": re_,
                       "high_amplification": am, "high_write_fanout": wf},
        "profile": {"request_count": n, "endpoint_template_count": m,
                    "read_event_count": len(reads), "write_event_count": len(writes),
                    "sampled_root_count": len(roots), "trace_depth": trace_depth,
                    "mean_unpruned_trace_nodes": baseline_mean},
        "table_metrics": table_metrics, "row_metrics": row_metrics,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-log", required=True)
    parser.add_argument("--read-log", required=True)
    parser.add_argument("--write-log", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--sample-size", type=int, default=128)
    parser.add_argument("--trace-depth", type=int, default=4)
    args = parser.parse_args()
    policy = build_noise_policy(load_request_logs(args.request_log), load_access_logs(args.read_log, "read"),
                                load_access_logs(args.write_log, "write"),
                                sample_size=args.sample_size, trace_depth=args.trace_depth)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(policy, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Noise policy written to {args.output}")
    print(f"classified {len(policy['block_tables'])} block tables, "
          f"{len(policy['block_rows'])} block rows, "
          f"{len(policy['gray_rows'])} gray rows")


if __name__ == "__main__":
    main()
