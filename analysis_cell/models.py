from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

RowKey = tuple[str, str]
CellKey = tuple[str, str, str]


@dataclass(frozen=True)
class RequestContext:
    rid: str
    api_id: str
    user_id: str
    identity_source: str = "unknown"


@dataclass(frozen=True)
class ReadEvent:
    time: datetime
    rid: str
    table: str
    pk: str
    sequence: int
    xmin: int | None = field(default=None, compare=False)
    xmax: int | None = field(default=None, compare=False)
    observed_cells: dict[str, Any] | None = field(default=None, compare=False)

    @property
    def row(self) -> RowKey:
        return self.table, self.pk


@dataclass(frozen=True)
class CellWrite:
    time: datetime
    rid: str
    operation: str
    table: str
    pk: str
    column: str
    value: Any = field(compare=False)
    sequence: int = 0
    xid: int | None = field(default=None, compare=False)

    @property
    def row(self) -> RowKey:
        return self.table, self.pk

    @property
    def cell(self) -> CellKey:
        return self.table, self.pk, self.column


@dataclass(frozen=True)
class RowDelete:
    time: datetime
    rid: str
    table: str
    pk: str
    sequence: int
    xid: int | None = field(default=None, compare=False)

    @property
    def row(self) -> RowKey:
        return self.table, self.pk


@dataclass(frozen=True)
class TaintDecision:
    tainted: bool
    confidence: str
    reason: str
    evidence: tuple[str, ...] = ()
    model: str = "basic"
    raw_response: str = ""


@dataclass(frozen=True)
class CellCandidate:
    child_rid: str
    cell: CellKey
    value: Any = field(compare=False)
    read_event: ReadEvent
    latest_write: CellWrite
    origin_write: CellWrite
    certainty: str = "certain"
    resolution_reason: str = "legacy-time"
    alternate_write: CellWrite | None = None


@dataclass(frozen=True)
class CellLink:
    parent_rid: str
    child_rid: str
    cell: CellKey
    value: Any = field(compare=False)
    read_event: ReadEvent
    latest_write: CellWrite
    origin_write: CellWrite
    decision: TaintDecision
    certainty: str = "certain"
    resolution_reason: str = "legacy-time"
    alternate_write: CellWrite | None = None
    task_priority: float | None = None
    task_rank: int | None = None


@dataclass(frozen=True)
class PriorityTaskInfo:
    child_rid: str
    cell: CellKey
    writer_rids: tuple[str, ...]
    read_time: datetime
    read_sequence: int
    writer_diversity: int
    writer_read_fanout: int
    rarity_score: float
    cost_score: float
    priority: float
    writer_identities: tuple[tuple[str, str], ...] = ()
    write_history_count: int = 1
    child_table_rows: int = 1
    child_table_cells: int = 1
    history_penalty: float = 1.0
    scan_penalty: float = 1.0
    compactness_bonus: float = 1.0
    base_priority: float | None = None


@dataclass(frozen=True)
class PrunedCell:
    candidate: CellCandidate
    decision: TaintDecision


@dataclass(frozen=True)
class UnresolvedCell:
    child_rid: str
    row: RowKey
    column: str | None
    read_time: datetime
    reason: str


@dataclass
class TraceResult:
    root_rid: str
    mode: str
    links: list[CellLink]
    visited_requests: set[str]
    pruned_cells: list[PrunedCell]
    unresolved_cells: list[UnresolvedCell]
    suppressed: dict[str, int] = field(default_factory=dict)
    budget_limit: int | None = None
    tasks_processed: int = 0
    tasks_discovered: int = 0
    pending_tasks: list[PriorityTaskInfo] = field(default_factory=list)
    queue_exhausted: bool = True
    priority_alpha: float | None = None
    priority_beta: float | None = None
    priority_diversity_mode: str | None = None
    request_contexts_loaded: int = 0

    @property
    def request_ids(self) -> set[str]:
        """Requests supported by recursively traversable, certain dependencies."""
        result = {self.root_rid}
        for link in self.links:
            if link.certainty == "certain":
                result.add(link.parent_rid)
                result.add(link.child_rid)
        return result

    @property
    def graph_request_ids(self) -> set[str]:
        result = {self.root_rid}
        for link in self.links:
            result.add(link.parent_rid)
            result.add(link.child_rid)
        return result

    @property
    def uncertain_request_ids(self) -> set[str]:
        return {
            link.parent_rid
            for link in self.links
            if link.certainty == "uncertain"
        }
