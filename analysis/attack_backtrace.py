import json
from datetime import datetime
from dateutil import parser as dtparser
from collections import defaultdict, deque
import bisect
from pathlib import Path
from typing import Dict, List, Tuple, Set, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------- Data structures ----------

# DataObject is uniquely identified by (table_name, primary_key); pk is normalized to str here.
DataObject = Tuple[str, str]

# Whether table-name matching is case-sensitive.
is_case_sensitive = False


def normalize_table_name(tbn: str) -> str:
    name = str(tbn).strip()
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    return name if is_case_sensitive else name.lower()

class WriteEvent:
    __slots__ = ("time", "rid", "event", "tbn", "pk")
    def __init__(self, time, rid, event, tbn, pk):
        self.time = time      # datetime
        self.rid = str(rid)   # str
        self.event = event    # "insert" / "update" / "delete"
        self.tbn = normalize_table_name(tbn)  # table name
        self.pk = str(pk)     # str

    def __repr__(self):
        return f"WriteEvent({self.time.isoformat()}, rid={self.rid}, {self.tbn}[{self.pk}], {self.event})"


class ReadEvent:
    __slots__ = ("time", "rid", "event", "tbn", "pk")
    def __init__(self, time, rid, event, tbn, pk):
        self.time = time
        self.rid = str(rid)
        self.event = event
        self.tbn = normalize_table_name(tbn)
        self.pk = str(pk)

    def __repr__(self):
        return f"ReadEvent({self.time.isoformat()}, rid={self.rid}, {self.tbn}[{self.pk}], {self.event})"


# ---------- Log loading and index construction ----------

def load_read_logs(path: str) -> List[ReadEvent]:
    reads = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            t = dtparser.parse(rec["time"])
            rid = rec["rid"]
            event = rec["event"]
            tbn = rec["tbn"]
            #if "session" in tbn: 
                #continue
            for pk in rec["pks"]:
                reads.append(ReadEvent(t, rid, event, tbn, str(pk)))
    # Sort globally by time.
    reads.sort(key=lambda r: r.time)
    return reads


def load_write_logs(path: str) -> List[WriteEvent]:
    writes = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            t = dtparser.parse(rec["time"])
            rid = rec["rid"]
            event = rec["event"]
            tbn = rec["tbn"]
            for pk in rec["pks"]:
                writes.append(WriteEvent(t, rid, event, tbn, str(pk)))
    writes.sort(key=lambda w: w.time)
    return writes


def build_indexes(reads: List[ReadEvent], writes: List[WriteEvent]):
    """
    1) writes_index[(tbn, pk)] = [WriteEvent...] sorted by ascending time
    2) writes_time_index[(tbn, pk)] = [datetime...]
    3) request_reads[rid] = set((tbn, pk), ...)
    4) request_read_events[rid] = List[ReadEvent]
    """
    writes_index: Dict[DataObject, List[WriteEvent]] = defaultdict(list)
    for w in writes:
        key = (w.tbn, w.pk)
        writes_index[key].append(w)

    writes_time_index: Dict[DataObject, List[datetime]] = {}
    for key, lst in writes_index.items():
        writes_time_index[key] = [w.time for w in lst]

    request_reads: Dict[str, Set[DataObject]] = defaultdict(set)
    request_read_events: Dict[str, List[ReadEvent]] = defaultdict(list)

    for r in reads:
        key = (r.tbn, r.pk)
        request_reads[r.rid].add(key)
        request_read_events[r.rid].append(r)

    return writes_index, writes_time_index, request_reads, request_read_events


# ---------- Predecessor write lookup ----------

def find_last_write_before(
    data_obj: DataObject,
    read_time: datetime,
    writes_index: Dict[DataObject, List[WriteEvent]],
    writes_time_index: Dict[DataObject, List[datetime]],
) -> Optional[WriteEvent]:
    """
    Return the latest write to data_obj before read_time.
    """
    if data_obj not in writes_index:
        return None
    times = writes_time_index[data_obj]
    idx = bisect.bisect_right(times, read_time) - 1
    if idx < 0:
        return None
    return writes_index[data_obj][idx]


# ---------- Build Request + DataObject causal graph ----------
def build_causal_graph_with_data(
    root_rid: str,
    writes_index: Dict[DataObject, List[WriteEvent]],
    writes_time_index: Dict[DataObject, List[datetime]],
    request_reads: Dict[str, Set[DataObject]],
    request_read_events: Dict[str, List[ReadEvent]],
):
    """
    Returns:
    - req_parents: Dict[child_rid, Set[parent_rid]]
    - req_to_data_writes: Dict[rid, Set[DataObject]]  rows written by the request
    - req_to_data_reads: Dict[rid, Set[DataObject]]   rows read by the request
    - data_writers: Dict[DataObject, Set[rid]]        requests that wrote the row
    - data_readers: Dict[DataObject, Set[rid]]        requests that read the row
    - read_write_links: List[(parent_rid, data_obj, child_rid, w_event, r_event)]
      detailed parent -> data -> child dependencies

    Important: multiple READ events for the same rid + DataObject are merged,
    keeping only one.
    """
    req_parents: Dict[str, Set[str]] = defaultdict(set)
    req_to_data_writes: Dict[str, Set[DataObject]] = defaultdict(set)
    req_to_data_reads: Dict[str, Set[DataObject]] = defaultdict(set)
    data_writers: Dict[DataObject, Set[str]] = defaultdict(set)
    data_readers: Dict[DataObject, Set[str]] = defaultdict(set)
    read_write_links = []

    visited_reqs: Set[str] = set()
    queue = deque([root_rid])

    # 1) Fill "request -> written data" and "data -> writer request" mappings.
    for data_obj, w_list in writes_index.items():
        for w in w_list:
            req_to_data_writes[w.rid].add(data_obj)
            data_writers[data_obj].add(w.rid)

    # 2) Fill "request -> read data" and "data -> reader request" mappings.
    for rid, dobjs in request_reads.items():
        for d in dobjs:
            req_to_data_reads[rid].add(d)
            data_readers[d].add(rid)

    # 3) Backtrace at the request level from root_rid.
    while queue:
        rid = queue.popleft()
        if rid in visited_reqs:
            continue
        visited_reqs.add(rid)

        if rid not in request_read_events:
            # This request has no read operations, or they were not recorded.
            continue

        # Merge ReadEvents for the same rid by DataObject.
        per_data_reads: Dict[DataObject, List[ReadEvent]] = defaultdict(list)
        for r_event in request_read_events[rid]:
            data_obj = (r_event.tbn, r_event.pk)
            per_data_reads[data_obj].append(r_event)

        # Keep one representative ReadEvent for each DataObject.
        for data_obj, r_events in per_data_reads.items():
            # Earliest or latest can be used; choose the earliest here.
            r_events.sort(key=lambda e: e.time)
            r_event = r_events[0]

            # Find the latest write to this row before the read.
            w_event = find_last_write_before(
                data_obj,
                r_event.time,
                writes_index,
                writes_time_index,
            )
            if w_event is None:
                # No predecessor write; treat this as a data source.
                continue

            parent_rid = w_event.rid
            if parent_rid == rid:
                # Same-request write then read: ignore it to avoid self-loops.
                continue

            # Record the causal relationship between requests; set deduplicates it.
            req_parents[rid].add(parent_rid)

            # Record detailed dependencies (parent -> data -> child).
            # This no longer emits multiple records for the same (rid, data_obj).
            read_write_links.append((parent_rid, data_obj, rid, w_event, r_event))

            if parent_rid not in visited_reqs:
                queue.append(parent_rid)

    return (
        req_parents,
        req_to_data_writes,
        req_to_data_reads,
        data_writers,
        data_readers,
        read_write_links,
    )


# ---------- Text tree printing with explicit Data nodes ----------

def print_causal_tree_with_data(
    root_rid: str,
    req_parents: Dict[str, Set[str]],
    read_write_links,
    indent: str = "",
):
    """
    Printed structure:

    R:<root_rid>
      <- D:table[pk]
           <- R:<parent_rid>
              ...

    read_write_links: List[(parent_rid, data_obj, child_rid, w_event, r_event)]
    """
    print(f"{indent}R:{root_rid}")

    # Find all dependencies where root_rid is the child.
    related_links = [l for l in read_write_links if l[2] == root_rid]
    if not related_links:
        return

    # Group by parent_rid for cleaner output.
    by_parent: Dict[str, List[Tuple[DataObject, WriteEvent, ReadEvent]]] = defaultdict(list)
    for parent_rid, data_obj, child_rid, w_event, r_event in related_links:
        by_parent[parent_rid].append((data_obj, w_event, r_event))

    for parent_rid, items in by_parent.items():
        for data_obj, w_event, r_event in items:
            tbn, pk = data_obj
            print(f"{indent}  <- D:{tbn}[{pk}]  (W:{w_event.event}@{w_event.time.isoformat()}, R:{r_event.event}@{r_event.time.isoformat()})")
            print(f"{indent}       <- R:{parent_rid}")
            # Recursively print predecessors of parent_rid.
            print_causal_tree_with_data(parent_rid, req_parents, read_write_links, indent + "          ")


# ---------- Graphviz export with Request + DataObject nodes ----------

def export_graphviz_with_data(
    root_rid: str,
    req_parents: Dict[str, Set[str]],
    read_write_links,
    out_path: str,
):
    """
    Draw the Request + DataObject causal tree:
    - The same Data row may appear multiple times; each parent-child dependency
      gets an independent Data node instance.
    - Top-to-bottom and left-to-right ordering approximately follows time.
    - Request nodes show rid + timestamp.
    """

    # ---------- 1. Compute each request timestamp from its earliest related event ----------
    req_times: Dict[str, datetime] = {}

    def update_req_time(rid: str, t: datetime):
        if rid not in req_times or t < req_times[rid]:
            req_times[rid] = t

    for parent_rid, data_obj, child_rid, w_event, r_event in read_write_links:
        update_req_time(parent_rid, w_event.time)
        update_req_time(child_rid, r_event.time)

    # ---------- 2. Find requests reachable from root_rid ----------
    reachable_reqs: Set[str] = set()
    q = deque([root_rid])

    while q:
        rid = q.popleft()
        if rid in reachable_reqs:
            continue
        reachable_reqs.add(rid)
        for p in req_parents.get(rid, []):
            if p not in reachable_reqs:
                q.append(p)

    # ---------- 3. Instantiate an independent Data node for each dependency ----------
    # edges: (src_node_id, dst_node_id, label)
    edges: List[Tuple[str, str, str]] = []
    req_nodes: Set[str] = set()
    data_instances: List[Tuple[str, str, str, datetime]] = []
    # (node_id, tbn, pk, time_for_layout)

    # Keep only dependencies related to reachable_reqs.
    link_index = 0
    for parent_rid, data_obj, child_rid, w_event, r_event in read_write_links:
        if child_rid not in reachable_reqs and parent_rid not in reachable_reqs:
            continue

        tbn, pk = data_obj

        # Instantiate a Data node.
        # Node IDs are unique, e.g.: D:dag[example]#0.
        data_node_id = f"D:{tbn}[{pk}]#{link_index}"
        link_index += 1

        # Use min(w_time, r_time) as this Data instance's layout time.
        data_time = min(w_event.time, r_event.time)
        data_instances.append((data_node_id, tbn, pk, data_time))

        # Parent and child request node IDs.
        parent_node_id = f"R:{parent_rid}"
        child_node_id = f"R:{child_rid}"
        req_nodes.add(parent_node_id)
        req_nodes.add(child_node_id)

        # Edges: parent -> data (WRITE), data -> child (READ).
        edges.append((parent_node_id, data_node_id, "WRITE"))
        edges.append((data_node_id, child_node_id, "READ"))

    # Filter out requests that are not connected to any edge.
    req_nodes = {rid for rid in req_nodes if rid.split(":", 1)[1] in reachable_reqs}

    # ---------- 4. Compute each node's layout time ----------
    # Request node time: req_times[rid].
    node_times: Dict[str, datetime] = {}

    for node_id in req_nodes:
        rid = node_id.split(":", 1)[1]
        t = req_times.get(rid)
        if t is None:
            # If a request has no collected time, place it at the bottom.
            t = datetime.max
        node_times[node_id] = t

    for node_id, tbn, pk, t in data_instances:
        node_times[node_id] = t

    # ---------- 5. Sort all nodes by time ----------
    all_nodes_sorted = sorted(node_times.items(), key=lambda kv: kv[1])
    # all_nodes_sorted: List[(node_id, time)]

    # Simple layering: split time-ordered nodes into chunks of N.
    # This can be changed to time-gap layering, e.g. one layer per minute.
    N = 6  # Roughly 6 nodes per layer; tune based on graph size.
    layers: List[List[str]] = []
    curr_layer: List[str] = []
    for i, (nid, _) in enumerate(all_nodes_sorted):
        curr_layer.append(nid)
        if len(curr_layer) >= N:
            layers.append(curr_layer)
            curr_layer = []
    if curr_layer:
        layers.append(curr_layer)

    # ---------- 6. Write DOT ----------
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("digraph causal_tree_with_repeated_data {\n")
        # Top to bottom: earlier events are above later events.
        f.write('  rankdir=TB;\n')
        f.write('  graph [ordering="out"];\n')
        f.write('  node [style="filled", fontsize=10];\n')

        # 6.1 Write node definitions first, preserving time order.
        defined_nodes: Set[str] = set()

        # Use rank=same groups to improve left-to-right time ordering within layers.
        for layer in layers:
            # Nodes within each layer are already sorted by time.
            f.write("  {")
            for nid in layer:
                # Define the node if it has not been defined yet.
                if nid not in defined_nodes:
                    if nid.startswith("R:"):
                        rid = nid.split(":", 1)[1]
                        t = req_times.get(rid)
                        if t is not None:
                            label = f"{nid}\\n{t.isoformat()}"
                        else:
                            label = nid

                        if rid == root_rid:
                            f.write(f'"{nid}" [label="{label}", shape=doubleoctagon, fillcolor="#ffddaa"]; ')
                        else:
                            f.write(f'"{nid}" [label="{label}", shape=box, fillcolor="#e0f0ff"]; ')
                    elif nid.startswith("D:"):
                        # Data node: find the label from data_instances.
                        # nid is D:table[pk]#idx; drop #idx in the displayed label.
                        base = nid.split("#", 1)[0]
                        label = base
                        f.write(f'"{nid}" [label="{label}", shape=ellipse, fillcolor="#e0ffe0"]; ')
                    defined_nodes.add(nid)
                else:
                    # Reference an already defined node again in rank=same.
                    f.write(f'"{nid}"; ')
            f.write("}\n")

        # 6.2 Write edges, sorted by endpoint time to help ordering.
        def edge_time(e):
            src, dst, lbl = e
            # Use the earlier endpoint time.
            return min(node_times[src], node_times[dst])

        edges_sorted = sorted(edges, key=edge_time)

        for src, dst, lbl in edges_sorted:
            if lbl == "WRITE":
                f.write(f'  "{src}" -> "{dst}" [label="WRITE", fontsize=9];\n')
            else:
                f.write(f'  "{src}" -> "{dst}" [label="READ", fontsize=9, style="dashed"];\n')

        f.write("}\n")

    print(f"Graphviz DOT with repeated Data nodes and time ordering written to: {out_path}")

# ---------- Main flow ----------

def main(
    read_log_path: str,
    write_log_path: str,
    root_rid: str,
):
    reads = load_read_logs(read_log_path)
    writes = load_write_logs(write_log_path)
    (
        writes_index,
        writes_time_index,
        request_reads,
        request_read_events,
    ) = build_indexes(reads, writes)

    print(f"Total read events: {len(reads)}, total write events: {len(writes)}")
    print(f"Distinct requests with reads: {len(request_read_events)}")

    (
        req_parents,
        req_to_data_writes,
        req_to_data_reads,
        data_writers,
        data_readers,
        read_write_links,
    ) = build_causal_graph_with_data(
        root_rid,
        writes_index,
        writes_time_index,
        request_reads,
        request_read_events,
    )

    #print("\n==== Text tree (Request + DataObject) ====\n")
    #print_causal_tree_with_data(root_rid, req_parents, read_write_links)

    dot_path = "causal_tree_with_data.dot"
    export_graphviz_with_data(root_rid, req_parents, read_write_links, dot_path)


if __name__ == "__main__":
    main(
        #read_log_path=str(REPO_ROOT / "examples" / "superset_test" / "logs" / "request_db_read.log"),
        #read_log_path=str(REPO_ROOT / "examples" / "moodle_test" / "moodle_data" / "db_access_audit.log"),
        #read_log_path=str(REPO_ROOT / "examples" / "admidio_test" / "logs" / "db_read.log"),
        read_log_path=str(REPO_ROOT / "examples" / "admidio5.0.5" / "logs" / "db_read.log"),
        #read_log_path=str(REPO_ROOT / "examples" / "dolphinscheduler" / "db_logs" / "read_provenance.log"),
        #read_log_path=str(REPO_ROOT / "examples" / "confluence_test" / "logs" / "read_provenance.log"),
        #read_log_path=str(REPO_ROOT / "examples" / "gitlab14.1" / "logs" / "db_read.log"),
        #read_log_path=str(REPO_ROOT / "examples" / "gitlab8.13" / "logs" / "db_read.log"),
        #read_log_path=str(REPO_ROOT / "examples" / "kanboard" / "logs" / "db_read.log"),
        #read_log_path=str(REPO_ROOT / "examples" / "craftcms" / "logs" / "db_read.log"),
        write_log_path=str(REPO_ROOT / "cdc_listener" / "request_db_write.log"),

        root_rid="640-1783950981-caf6b62f", # Final attack request ID
    )
