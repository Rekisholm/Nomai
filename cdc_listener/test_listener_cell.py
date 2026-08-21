import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))
import listener_cell  # noqa: E402


class _Cursor:
    def __init__(self):
        self.flush_lsn = None

    def send_feedback(self, *, flush_lsn):
        self.flush_lsn = flush_lsn


class _Message:
    def __init__(self, payload):
        self.payload = json.dumps(payload)
        self.data_start = 99
        self.cursor = _Cursor()


class CellListenerTests(unittest.TestCase):
    def test_consumer_persists_transaction_xid_and_change_sequence(self):
        message = _Message({
            "xid": 42,
            "timestamp": "2026-01-01T00:00:00Z",
            "change": [
                {
                    "kind": "message",
                    "prefix": "request",
                    "content": json.dumps({"request_id": "R1"}),
                },
                {
                    "kind": "insert",
                    "schema": "public",
                    "table": "items",
                    "columnnames": ["id", "payload"],
                    "columnvalues": [1, "value"],
                },
            ],
        })

        with patch.object(listener_cell, "TARGETS", {"public.items": "id"}), patch.object(
            listener_cell.logging, "info"
        ) as log_info:
            listener_cell.consume(message)

        record = json.loads(log_info.call_args.args[0])
        self.assertEqual(record["rid"], "R1")
        self.assertEqual(record["xid"], 42)
        self.assertEqual(record["seq"], 0)
        self.assertEqual(record["cells"][1], {"col": "payload", "new": "value"})
        self.assertEqual(message.cursor.flush_lsn, 99)


if __name__ == "__main__":
    unittest.main()
