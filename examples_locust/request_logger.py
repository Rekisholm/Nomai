import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


UUID_RE = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
NUMERIC_ID_RE = re.compile(r"^\d+$")
UUID_SEGMENT_RE = re.compile(r"^" + UUID_RE + r"$", re.IGNORECASE)


def normalize_request_path(url):
    parts = urlsplit(url)
    normalized_segments = []
    for segment in parts.path.split("/"):
        if NUMERIC_ID_RE.match(segment) or UUID_SEGMENT_RE.match(segment):
            normalized_segments.append("{id}")
        else:
            normalized_segments.append(segment)

    normalized_query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in {"timestamp", "_"}:
            value = "{time}"
        elif NUMERIC_ID_RE.match(value) or UUID_SEGMENT_RE.match(value):
            value = "{id}"
        normalized_query.append((key, value))

    query = urlencode(normalized_query, doseq=True, safe="{}")
    return urlunsplit(("", "", "/".join(normalized_segments), query, ""))


class RequestLogger:
    def __init__(self, log_dir):
        self.log_dir = Path(log_dir)
        self.file = None
        self.lock = threading.Lock()

    def open(self):
        if self.file is None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = self.log_dir / f"request_log_{run_id}_{os.getpid()}.jsonl"
            self.file = path.open("a", encoding="utf-8")
        return self.file.name

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None

    def write(self, rid, request_time, method, path):
        if self.file is None:
            self.open()

        record = {
            "rid": rid,
            "time": datetime.fromtimestamp(request_time, timezone.utc).isoformat(),
            "method": method.upper(),
            "path": normalize_request_path(path),
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            self.file.write(line + "\n")
            self.file.flush()

    def write_response(self, response, request_time, method, path):
        self.write(response.headers.get("X-Request-Id"), request_time, method, path)

    def install(self, events):
        @events.test_start.add_listener
        def on_request_log_start(environment, **kwargs):
            print(f"Request log: {self.open()}")

        @events.test_stop.add_listener
        def on_request_log_stop(environment, **kwargs):
            self.close()
