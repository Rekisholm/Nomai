from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from analysis.attack_backtrace_pruning import Policy, build_pruned_trace
from analysis.pruning_algorithm import (
    AccessEvent,
    RequestRecord,
    build_noise_policy,
    load_access_logs,
    normalize_path,
)


class PruningAlgorithmTests(unittest.TestCase):
    def test_normalize_path_uses_only_identifier_shape(self) -> None:
        self.assertEqual(
            normalize_path("/project/123/item/550e8400-e29b-41d4-a716-446655440000?q=x"),
            "/project/{id}/item/{uuid}",
        )

    def test_loader_accepts_scalar_documented_schema(self) -> None:
        record = {
            "rid": "r1",
            "time": "2026-01-01T00:00:00Z",
            "table": "Example",
            "pk": 7,
            "op": "update",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "write.jsonl")
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            events = load_access_logs(path, "write")
        self.assertEqual(events[0].data_object, ("example", "7"))
        self.assertEqual(events[0].operation, "update")

    def test_profile_finds_structural_chain_without_name_signal(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        requests = [
            RequestRecord(f"r{i}", start + timedelta(seconds=i), "GET", f"/view/{i}")
            for i in range(30)
        ]
        writes = [
            AccessEvent(requests[0].time, "r0", "runtime_x", "1", "write"),
            AccessEvent(requests[1].time, "r1", "runtime_x", "2", "write"),
        ]
        reads = [
            AccessEvent(requests[1].time, "r1", "runtime_x", "1", "read"),
            *[
                AccessEvent(request.time, request.rid, "runtime_x", "2", "read")
                for request in requests[2:]
            ],
        ]
        policy = build_noise_policy(requests, reads, writes, sample_size=30, trace_depth=4)
        self.assertIn("runtime_x", policy["block_tables"])
        self.assertIn("runtime_x[2]", policy["block_rows"])

    def test_high_coverage_read_only_table_is_not_blocked(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        requests = [
            RequestRecord(f"r{i}", start + timedelta(seconds=i), "GET", f"/view/{i}")
            for i in range(5)
        ]
        reads = [
            AccessEvent(request.time, request.rid, "static_x", "1", "read")
            for request in requests
        ]
        policy = build_noise_policy(requests, reads, [], sample_size=5, trace_depth=4)
        self.assertNotIn("static_x", policy["block_tables"])

    def test_backtrace_blocks_noise_and_keeps_business_dependency(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        writes = [
            AccessEvent(start, "noise-parent", "runtime_x", "1", "update"),
            AccessEvent(start, "attack-parent", "payload_x", "9", "insert"),
        ]
        reads = [
            AccessEvent(start + timedelta(seconds=1), "root", "runtime_x", "1", "read"),
            AccessEvent(start + timedelta(seconds=1), "root", "payload_x", "9", "read"),
        ]
        policy = Policy(
            frozenset({"runtime_x"}), frozenset(), frozenset(), frozenset()
        )
        result = build_pruned_trace("root", reads, writes, policy)
        self.assertEqual({link.parent_rid for link in result.links}, {"attack-parent"})
        self.assertEqual(result.suppressed_by_reason, {"block-table": 1})

    def test_gray_dependency_is_visible_but_does_not_expand(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        writes = [
            AccessEvent(start, "gray-source", "context_x", "1", "update"),
            AccessEvent(start, "business-source", "input_x", "2", "insert"),
            AccessEvent(start + timedelta(seconds=2), "middle", "payload_x", "9", "insert"),
        ]
        reads = [
            AccessEvent(start + timedelta(seconds=1), "middle", "context_x", "1", "read"),
            AccessEvent(start + timedelta(seconds=1), "middle", "input_x", "2", "read"),
            AccessEvent(start + timedelta(seconds=3), "root", "payload_x", "9", "read"),
        ]
        policy = Policy(
            frozenset(), frozenset(), frozenset(), frozenset({("context_x", "1")})
        )
        result = build_pruned_trace("root", reads, writes, policy)
        gray_link = next(link for link in result.links if link.decision == "gray")
        self.assertFalse(gray_link.recurse)
        self.assertNotIn("gray-source", result.visited_requests)
        self.assertIn("gray-source", result.request_ids)


if __name__ == "__main__":
    unittest.main()
