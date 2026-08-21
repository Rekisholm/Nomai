from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass
from itertools import count
from typing import Mapping

from .core import CellIndexes, _request_candidates
from .models import (
    CellCandidate,
    CellLink,
    PriorityTaskInfo,
    RequestContext,
    TaintDecision,
    TraceResult,
    UnresolvedCell,
)


@dataclass(frozen=True)
class _PriorityTask:
    candidates: tuple[CellCandidate, ...]
    info: PriorityTaskInfo

    @property
    def key(self) -> tuple[object, ...]:
        first = self.candidates[0]
        return (
            first.child_rid,
            first.cell,
            first.read_event.sequence,
            first.read_event.time,
            tuple(
                sorted(
                    [
                        (
                            candidate.origin_write.rid,
                            candidate.latest_write.xid,
                            candidate.latest_write.sequence,
                            candidate.certainty,
                        )
                        for candidate in self.candidates
                    ],
                    key=repr,
                ),
            ),
        )


def _writer_identity(
    rid: str,
    request_contexts: Mapping[str, RequestContext] | None,
) -> tuple[str, str]:
    if request_contexts is not None:
        context = request_contexts.get(rid)
        if context is not None:
            return context.user_id, context.api_id
    # Backward-compatible v1 fallback: every request is a distinct writer.
    return f"request:{rid}", "unknown-api"


def _writer_diversity(
    indexes: CellIndexes,
    request_contexts: Mapping[str, RequestContext] | None,
) -> tuple[
    dict[tuple[str, str, str], int],
    dict[tuple[str, str, str], tuple[tuple[str, str], ...]],
]:
    """Count unique (user, API) origin writers after same-value merging."""
    result: dict[tuple[str, str, str], int] = {}
    identities_by_cell: dict[
        tuple[str, str, str], tuple[tuple[str, str], ...]
    ] = {}
    for cell, history in indexes.histories.items():
        if cell in indexes.xid_histories:
            history = indexes.xid_histories[cell]
            origin_positions = indexes.xid_origin_positions[cell]
        else:
            origin_positions = indexes.origin_positions[cell]
        identities = tuple(sorted(
            {
                _writer_identity(history[position].rid, request_contexts)
                for position in origin_positions
            }
        ))
        result[cell] = len(identities)
        identities_by_cell[cell] = identities
    return result, identities_by_cell


def _read_fanout(indexes: CellIndexes) -> dict[str, int]:
    """Precompute |ReadSet(rid)| as the number of distinct rows read."""
    return {
        rid: len({event.row for event in events})
        for rid, events in indexes.request_reads.items()
    }


def _write_history_count(indexes: CellIndexes) -> dict[tuple[str, str, str], int]:
    """Count write events retained in each cell history, including replays."""
    return {
        cell: max(1, len(indexes.xid_histories.get(cell, history)))
        for cell, history in indexes.histories.items()
    }


def _child_table_breadth(
    indexes: CellIndexes,
) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], int]]:
    """Count distinct rows and attributable cells read per request/table."""
    rows_by_request_table: dict[
        tuple[str, str], set[tuple[str, str]]
    ] = {}
    for rid, events in indexes.request_reads.items():
        for event in events:
            key = (rid, event.table)
            rows_by_request_table.setdefault(key, set()).add(event.row)
    row_counts = {
        key: max(1, len(rows))
        for key, rows in rows_by_request_table.items()
    }
    cell_counts: dict[tuple[str, str], int] = {}
    for key, rows in rows_by_request_table.items():
        cells: set[tuple[str, str, str]] = set()
        for row in rows:
            cells.update(indexes.row_cells.get(row, ()))
        cell_counts[key] = max(1, len(cells))
    return row_counts, cell_counts


def _group_candidates(
    candidates: list[CellCandidate],
) -> list[tuple[CellCandidate, ...]]:
    """One queue task represents one cell read and its 1--2 attributions."""
    grouped: dict[tuple[object, ...], list[CellCandidate]] = {}
    for candidate in candidates:
        key = (
            candidate.child_rid,
            candidate.cell,
            candidate.read_event.sequence,
            candidate.read_event.time,
        )
        grouped.setdefault(key, []).append(candidate)
    return [tuple(items) for items in grouped.values()]


def _make_task(
    candidates: tuple[CellCandidate, ...],
    *,
    writer_diversity: dict[tuple[str, str, str], int],
    writer_identities: dict[
        tuple[str, str, str], tuple[tuple[str, str], ...]
    ],
    read_fanout: dict[str, int],
    write_history_count: dict[tuple[str, str, str], int],
    child_table_breadth: dict[tuple[str, str], int],
    child_table_cells: dict[tuple[str, str], int],
    alpha: float,
    beta: float,
) -> _PriorityTask:
    first = candidates[0]
    writer_rids = tuple(sorted({item.origin_write.rid for item in candidates}))
    diversity = writer_diversity[first.cell]
    fanout = sum(read_fanout.get(rid, 0) for rid in writer_rids)
    rarity_score = 1.0 / (1.0 + math.log(1.0 + diversity))
    cost_score = 1.0 / (1.0 + math.log(1.0 + fanout))
    base_priority = alpha * rarity_score + beta * cost_score
    history_count = write_history_count[first.cell]
    table_rows = child_table_breadth.get(
        (first.child_rid, first.cell[0]),
        1,
    )
    table_cells = child_table_cells.get(
        (first.child_rid, first.cell[0]),
        1,
    )
    # Repeated same-origin state and broad child-side scans can create many
    # deceptively rare tasks. Normalize both effects without deleting any
    # candidate. Exactly two rows are treated as a bounded relationship lookup
    # and ranked by their attributable cell footprint.
    maximum_component = 1.0 / (1.0 + math.log(2.0))
    history_score = 1.0 / (1.0 + math.log(1.0 + history_count))
    scan_score = 1.0 / (1.0 + math.log(1.0 + table_rows))
    history_penalty = history_score / maximum_component
    if table_rows == 2:
        scan_penalty = 1.0
        compactness_bonus = 1.0 + 1.0 / (1.0 + table_cells)
    else:
        scan_penalty = scan_score / maximum_component
        compactness_bonus = 1.0
    priority = (
        base_priority * history_penalty * scan_penalty
        * compactness_bonus
    )
    return _PriorityTask(
        candidates=candidates,
        info=PriorityTaskInfo(
            child_rid=first.child_rid,
            cell=first.cell,
            writer_rids=writer_rids,
            read_time=first.read_event.time,
            read_sequence=first.read_event.sequence,
            writer_diversity=diversity,
            writer_read_fanout=fanout,
            rarity_score=rarity_score,
            cost_score=cost_score,
            priority=priority,
            writer_identities=writer_identities[first.cell],
            write_history_count=history_count,
            child_table_rows=table_rows,
            child_table_cells=table_cells,
            history_penalty=history_penalty,
            scan_penalty=scan_penalty,
            compactness_bonus=compactness_bonus,
            base_priority=base_priority,
        ),
    )


def _build_scheduled_trace(
    root_rid: str,
    indexes: CellIndexes,
    *,
    budget: int | None = None,
    alpha: float = 0.5,
    beta: float = 0.5,
    use_priority: bool = True,
    random_seed: int | None = None,
    request_contexts: Mapping[str, RequestContext] | None = None,
) -> TraceResult:
    """Backtrace cell tasks in descending priority without deleting any task.

    ``budget`` counts unique cell tasks popped from the priority queue. ``None``
    exhausts the queue and therefore produces the same certain dependency
    closure as basic mode, modulo traversal order.
    """
    root_rid = str(root_rid)
    if budget is not None and budget < 0:
        raise ValueError("budget must be non-negative")
    if alpha < 0 or beta < 0 or not math.isclose(alpha + beta, 1.0):
        raise ValueError("alpha and beta must be non-negative and sum to 1")
    diversity, identities = _writer_diversity(indexes, request_contexts)
    fanout = _read_fanout(indexes)
    history_count = _write_history_count(indexes)
    table_breadth, table_cells = _child_table_breadth(indexes)
    rng = random.Random(random_seed) if random_seed is not None else None
    heap: list[tuple[float, int, int, _PriorityTask]] = []
    tie_breaker = count()
    seen_tasks: set[tuple[object, ...]] = set()
    expanded_requests: set[str] = {root_rid}
    discovered_request_edges: set[tuple[str, str]] = set()
    links: list[CellLink] = []
    unresolved: list[UnresolvedCell] = []
    tasks_discovered = 0

    def expansion_tier(task: _PriorityTask) -> int:
        """Delay cell tasks whose certain request edges were already exposed.

        Multiple cells from one row often attribute to the same writer.  Once
        one of them has exposed the same parent--child request edge, processing
        its siblings cannot extend the request graph.  Keep them for exhaustive
        closure, but place them behind tasks that can still expose a new edge.
        """
        certain_edges = {
            (candidate.origin_write.rid, candidate.child_rid)
            for candidate in task.candidates
            if candidate.certainty == "certain"
        }
        return int(
            bool(certain_edges) and certain_edges <= discovered_request_edges
        )

    def enqueue_request(rid: str) -> None:
        nonlocal tasks_discovered
        candidates, request_unresolved = _request_candidates(rid, indexes)
        unresolved.extend(request_unresolved)
        for candidate_group in _group_candidates(candidates):
            task = _make_task(
                candidate_group,
                writer_diversity=diversity,
                writer_identities=identities,
                read_fanout=fanout,
                write_history_count=history_count,
                child_table_breadth=table_breadth,
                child_table_cells=table_cells,
                alpha=alpha,
                beta=beta,
            )
            if rng is not None:
                sort_key = rng.random()
            elif use_priority:
                sort_key = -task.info.priority
            else:
                sort_key = 0.0
            heapq.heappush(
                heap,
                (
                    sort_key,
                    expansion_tier(task) if use_priority and rng is None else 0,
                    next(tie_breaker),
                    task,
                ),
            )
            tasks_discovered += 1

    enqueue_request(root_rid)
    tasks_processed = 0

    while heap and (budget is None or tasks_processed < budget):
        sort_key, queued_tier, _, task = heapq.heappop(heap)
        if use_priority and rng is None:
            current_tier = expansion_tier(task)
            if current_tier > queued_tier:
                heapq.heappush(
                    heap,
                    (sort_key, current_tier, next(tie_breaker), task),
                )
                continue
        if task.key in seen_tasks:
            continue
        seen_tasks.add(task.key)
        tasks_processed += 1

        for candidate in task.candidates:
            parent_rid = candidate.origin_write.rid
            diversity_label = (
                "rho(user+api)"
                if request_contexts is not None
                else "rho_v1(rid)"
            )
            reason = (
                f"priority: {diversity_label}="
                f"{task.info.rarity_score:.6f}, "
                f"kappa={task.info.cost_score:.6f}, "
                f"history_penalty={task.info.history_penalty:.6f}, "
                f"scan_penalty={task.info.scan_penalty:.6f}, "
                f"compactness_bonus={task.info.compactness_bonus:.6f}, "
                f"score={task.info.priority:.6f}"
            )
            if parent_rid == candidate.child_rid:
                reason = "[self-write] " + reason
            if candidate.certainty == "uncertain":
                reason = "[uncertain-concurrent-window-no-recursion] " + reason
            links.append(CellLink(
                parent_rid=parent_rid,
                child_rid=candidate.child_rid,
                cell=candidate.cell,
                value=candidate.value,
                read_event=candidate.read_event,
                latest_write=candidate.latest_write,
                origin_write=candidate.origin_write,
                decision=TaintDecision(
                    tainted=True,
                    confidence="not-applicable",
                    reason=reason,
                    model="priority",
                ),
                certainty=candidate.certainty,
                resolution_reason=candidate.resolution_reason,
                alternate_write=candidate.alternate_write,
                task_priority=task.info.priority,
                task_rank=tasks_processed,
            ))

            if candidate.certainty != "certain":
                continue
            discovered_request_edges.add((parent_rid, candidate.child_rid))
            if parent_rid in expanded_requests:
                continue
            expanded_requests.add(parent_rid)
            enqueue_request(parent_rid)

    pending = [item[3].info for item in sorted(heap)]
    return TraceResult(
        root_rid=root_rid,
        mode="priority" if use_priority else "fifo",
        links=links,
        visited_requests=expanded_requests,
        pruned_cells=[],
        unresolved_cells=unresolved,
        budget_limit=budget,
        tasks_processed=tasks_processed,
        tasks_discovered=tasks_discovered,
        pending_tasks=pending,
        queue_exhausted=not heap,
        priority_alpha=alpha,
        priority_beta=beta,
        priority_diversity_mode=(
            "user_api" if request_contexts is not None else "request_id"
        ),
        request_contexts_loaded=len(request_contexts or {}),
    )


def build_priority_trace(
    root_rid: str,
    indexes: CellIndexes,
    *,
    budget: int | None = None,
    alpha: float = 0.5,
    beta: float = 0.5,
    request_contexts: Mapping[str, RequestContext] | None = None,
) -> TraceResult:
    return _build_scheduled_trace(
        root_rid,
        indexes,
        budget=budget,
        alpha=alpha,
        beta=beta,
        use_priority=True,
        request_contexts=request_contexts,
    )


def build_fifo_trace(
    root_rid: str,
    indexes: CellIndexes,
    *,
    budget: int | None = None,
) -> TraceResult:
    """FIFO baseline using the same cell-task budget and expansion semantics."""
    return _build_scheduled_trace(
        root_rid,
        indexes,
        budget=budget,
        alpha=0.5,
        beta=0.5,
        use_priority=False,
    )


def build_random_trace(
    root_rid: str,
    indexes: CellIndexes,
    *,
    budget: int | None = None,
    random_seed: int = 0,
) -> TraceResult:
    """Random baseline using random task ordering with a fixed seed."""
    return _build_scheduled_trace(
        root_rid,
        indexes,
        budget=budget,
        alpha=0.5,
        beta=0.5,
        use_priority=False,
        random_seed=random_seed,
    )
