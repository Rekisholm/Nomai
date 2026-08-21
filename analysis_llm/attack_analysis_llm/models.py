from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Tuple

DataObject = Tuple[str, str]


@dataclass(frozen=True)
class ReadEvent:
    time: datetime
    rid: str
    tbn: str
    pk: str


@dataclass(frozen=True)
class WriteEvent:
    time: datetime
    rid: str
    event: str
    tbn: str
    pk: str
    row: Dict[str, Any]


@dataclass(frozen=True)
class TaintDecision:
    is_tainted: bool
    confidence: str
    reason: str
    evidence: List[str]
    model: str
    raw_response: str


@dataclass(frozen=True)
class DependencyLink:
    parent_rid: str
    child_rid: str
    data_obj: DataObject
    write_event: WriteEvent
    read_event: ReadEvent
    decision: TaintDecision


@dataclass(frozen=True)
class PrunedRead:
    rid: str
    data_obj: DataObject
    read_event: ReadEvent
    checked_writers: List[str]
