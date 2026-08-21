from __future__ import annotations

import bisect
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .analyzers import BaseTaintAnalyzer
from .models import DataObject, DependencyLink, PrunedRead, ReadEvent, WriteEvent


def coalesce_request_reads(
    read_events: Sequence[ReadEvent],
    selection: str,
) -> Dict[DataObject, ReadEvent]:
    grouped: Dict[DataObject, List[ReadEvent]] = defaultdict(list)
    for event in read_events:
        grouped[(event.tbn, event.pk)].append(event)

    chosen: Dict[DataObject, ReadEvent] = {}
    for data_obj, events in grouped.items():
        events = sorted(events, key=lambda item: item.time)
        chosen[data_obj] = events[0] if selection == "earliest" else events[-1]
    return chosen


def find_last_write_before(
    data_obj: DataObject,
    read_time: datetime,
    writes_index: Dict[DataObject, List[WriteEvent]],
    writes_time_index: Dict[DataObject, List[datetime]],
) -> Optional[WriteEvent]:
    if data_obj not in writes_index:
        return None
    times = writes_time_index[data_obj]
    idx = bisect.bisect_right(times, read_time) - 1
    if idx < 0:
        return None
    return writes_index[data_obj][idx]


def build_taint_backtrace(
    *,
    root_rid: str,
    attack_description: str,
    analyzer: BaseTaintAnalyzer,
    writes_index: Dict[DataObject, List[WriteEvent]],
    writes_time_index: Dict[DataObject, List[datetime]],
    request_read_events: Dict[str, List[ReadEvent]],
    read_selection: str,
    max_candidates_per_read: Optional[int],
) -> Tuple[
    Dict[str, Set[str]],
    List[DependencyLink],
    List[PrunedRead],
    List[Dict[str, Any]],
]:
    req_parents: Dict[str, Set[str]] = defaultdict(set)
    links: List[DependencyLink] = []
    pruned_reads: List[PrunedRead] = []
    decision_audit: List[Dict[str, Any]] = []

    queue: deque[str] = deque([root_rid])
    expanded_requests: Set[str] = set()

    while queue:
        rid = queue.popleft()
        if rid in expanded_requests:
            continue
        expanded_requests.add(rid)

        selected_reads = coalesce_request_reads(request_read_events.get(rid, []), selection=read_selection)

        batch_items: List[Tuple[DataObject, ReadEvent, WriteEvent]] = []
        for data_obj, read_event in selected_reads.items():
            write_event = find_last_write_before(
                data_obj=data_obj,
                read_time=read_event.time,
                writes_index=writes_index,
                writes_time_index=writes_time_index,
            )
            if write_event is None:
                continue
            if write_event.rid == rid:
                continue
            batch_items.append((data_obj, read_event, write_event))

        if not batch_items:
            continue

        decisions = analyzer.judge_batch(
            root_rid=root_rid,
            attack_description=attack_description,
            child_rid=rid,
            items=batch_items,
        )

        for (data_obj, read_event, write_event), decision in zip(batch_items, decisions):
            decision_audit.append({
                "child_rid": rid,
                "parent_rid": write_event.rid,
                "table_name": data_obj[0],
                "pk": data_obj[1],
                "write_time": write_event.time.isoformat(),
                "read_time": read_event.time.isoformat(),
                "tainted": decision.is_tainted,
                "confidence": decision.confidence,
                "reason": decision.reason,
                "model": decision.model,
                "evidence": decision.evidence,
            })

            if decision.is_tainted:
                req_parents[rid].add(write_event.rid)
                links.append(DependencyLink(
                    parent_rid=write_event.rid,
                    child_rid=rid,
                    data_obj=data_obj,
                    write_event=write_event,
                    read_event=read_event,
                    decision=decision,
                ))
                if write_event.rid not in expanded_requests:
                    queue.append(write_event.rid)
            else:
                pruned_reads.append(PrunedRead(
                    rid=rid,
                    data_obj=data_obj,
                    read_event=read_event,
                    checked_writers=[write_event.rid],
                ))

    return req_parents, links, pruned_reads, decision_audit
