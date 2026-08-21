from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Tuple

from .common import extract_json_object, flatten_row_text
from .models import DataObject, ReadEvent, TaintDecision, WriteEvent


class BaseTaintAnalyzer:
    def __init__(self, model_name: str):
        self.model_name = model_name
        self._cache: Dict[str, TaintDecision] = {}
        self._batch_cache: Dict[str, List[TaintDecision]] = {}

    def judge(
        self,
        *,
        root_rid: str,
        attack_description: str,
        child_rid: str,
        data_obj: DataObject,
        read_event: ReadEvent,
        write_event: WriteEvent,
    ) -> TaintDecision:
        payload = {
            "root_rid": root_rid,
            "attack_description": attack_description,
            "child_rid": child_rid,
            "data_obj": data_obj,
            "read_time": read_event.time.isoformat(),
            "write_rid": write_event.rid,
            "write_time": write_event.time.isoformat(),
            "event": write_event.event,
            "row": write_event.row,
        }
        cache_key = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        if cache_key not in self._cache:
            self._cache[cache_key] = self._judge_uncached(
                root_rid=root_rid,
                attack_description=attack_description,
                child_rid=child_rid,
                data_obj=data_obj,
                read_event=read_event,
                write_event=write_event,
            )
        return self._cache[cache_key]

    def _judge_uncached(
        self,
        *,
        root_rid: str,
        attack_description: str,
        child_rid: str,
        data_obj: DataObject,
        read_event: ReadEvent,
        write_event: WriteEvent,
    ) -> TaintDecision:
        raise NotImplementedError

    def judge_batch(
        self,
        *,
        root_rid: str,
        attack_description: str,
        child_rid: str,
        items: List[Tuple[DataObject, ReadEvent, WriteEvent]],
    ) -> List[TaintDecision]:
        payload = {
            "root_rid": root_rid,
            "attack_description": attack_description,
            "child_rid": child_rid,
            "items": [
                {
                    "data_obj": data_obj,
                    "read_time": read_event.time.isoformat(),
                    "write_rid": write_event.rid,
                    "write_time": write_event.time.isoformat(),
                    "event": write_event.event,
                    "row": write_event.row,
                }
                for data_obj, read_event, write_event in items
            ],
        }
        cache_key = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        if cache_key not in self._batch_cache:
            self._batch_cache[cache_key] = self._judge_batch_uncached(
                root_rid=root_rid,
                attack_description=attack_description,
                child_rid=child_rid,
                items=items,
            )
        return self._batch_cache[cache_key]

    def _judge_batch_uncached(
        self,
        *,
        root_rid: str,
        attack_description: str,
        child_rid: str,
        items: List[Tuple[DataObject, ReadEvent, WriteEvent]],
    ) -> List[TaintDecision]:
        return [
            self._judge_uncached(
                root_rid=root_rid,
                attack_description=attack_description,
                child_rid=child_rid,
                data_obj=data_obj,
                read_event=read_event,
                write_event=write_event,
            )
            for data_obj, read_event, write_event in items
        ]


class HeuristicTaintAnalyzer(BaseTaintAnalyzer):
    SUSPICIOUS_MARKERS = [
        "runtime.getruntime().exec",
        "processbuilder(",
        "cat /etc/passwd",
        "curl ",
        "wget ",
        "bash -c",
        "sh -c",
        "powershell",
        "nc -e",
        "rm -rf",
        "select * from",
        "union select",
        "<script",
        "javascript:",
        "cmd.exe",
        "/tmp/attack",
        "payload",
        "attack",
    ]

    def __init__(self) -> None:
        super().__init__(model_name="heuristic")

    def _judge_uncached(
        self,
        *,
        root_rid: str,
        attack_description: str,
        child_rid: str,
        data_obj: DataObject,
        read_event: ReadEvent,
        write_event: WriteEvent,
    ) -> TaintDecision:
        _ = (root_rid, attack_description, child_rid, data_obj, read_event)
        row_text = flatten_row_text(write_event.row).lower()
        hits = [marker for marker in self.SUSPICIOUS_MARKERS if marker in row_text]
        return TaintDecision(
            is_tainted=bool(hits),
            confidence="medium" if hits else "high",
            reason=(
                f"Matched heuristic suspicious markers: {', '.join(hits[:5])}"
                if hits else "No heuristic taint markers matched"
            ),
            evidence=hits[:5],
            model=self.model_name,
            raw_response=json.dumps({"heuristic_hits": hits}, ensure_ascii=False),
        )


class DeepSeekTaintAnalyzer(BaseTaintAnalyzer):
    def __init__(self, api_key: str, model_name: str, api_url: str, timeout_seconds: int) -> None:
        super().__init__(model_name=model_name)
        self.api_key = api_key
        self.api_url = api_url
        self.timeout_seconds = timeout_seconds

    def _judge_uncached(
        self,
        *,
        root_rid: str,
        attack_description: str,
        child_rid: str,
        data_obj: DataObject,
        read_event: ReadEvent,
        write_event: WriteEvent,
    ) -> TaintDecision:
        table_name, pk = data_obj
        system_prompt = (
            "You are a security provenance analysis assistant. "
            "Your task is to decide whether the full row content after a database write "
            "is tainted data that may propagate to the final attack request. "
            "Always output valid JSON."
        )
        user_prompt = f"""
Use the context below to decide whether this write event should be treated as a tainted predecessor in the attack request chain.

Decision criteria:
1. If the full row contains a malicious command, attack script, dangerous expression, injection payload, or malicious control data that may propagate to a later attack request, set tainted=true.
2. If the row only contains ordinary business fields, benign configuration, no attack-related context, or no evidence of attack propagation, set tainted=false.
3. For delete events, set tainted=false unless the event contains effective content supporting attack propagation.
4. Be conservative and keep only writes that are genuinely likely to be related to attack propagation.

EXAMPLE INPUT:
Final attack request: req-attack-123
Attack context: The final request has been confirmed as an attack request. Keep only upstream database writes that truly carry or propagate the attack payload.
Current backtraced request: req-upstream-456
Current read event time: 2026-06-10T12:00:00+08:00
Current data row read: users[1]
Candidate write event:
- request_id: req-write-789
- time: 2026-06-10T11:50:00+08:00
- event: update
- table: users
- pk: 1
- row_after_write_json: {{"id": 1, "name": "admin", "bio": "<script>alert(1)</script>"}}

EXAMPLE JSON OUTPUT:
{{
  "tainted": true,
  "confidence": "high",
  "reason": "The written bio field contains an XSS attack script that may be read and executed by a later attack request.",
  "evidence": ["The bio field value is '<script>alert(1)</script>'."]
}}

ACTUAL INPUT:
Final attack request: {root_rid}
Attack context: {attack_description}
Current backtraced request: {child_rid}
Current read event time: {read_event.time.isoformat()}
Current data row read: {table_name}[{pk}]

Candidate write event:
- request_id: {write_event.rid}
- time: {write_event.time.isoformat()}
- event: {write_event.event}
- table: {write_event.tbn}
- pk: {write_event.pk}
- row_after_write_json: {json.dumps(write_event.row, ensure_ascii=False, sort_keys=True, default=str)}

Output the corresponding JSON OUTPUT:
""".strip()

        body = {
            "model": self.model_name,
            "temperature": 0,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        request = urllib.request.Request(
            self.api_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw_bytes = response.read()
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"DeepSeek API returned HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DeepSeek API request failed: {exc}") from exc

        payload = json.loads(raw_bytes.decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        
        decision_json = extract_json_object(content)
        evidence = decision_json.get("evidence", [])
        if not isinstance(evidence, list):
            evidence = [str(evidence)]
        return TaintDecision(
            is_tainted=bool(decision_json.get("tainted")),
            confidence=str(decision_json.get("confidence", "unknown")),
            reason=str(decision_json.get("reason", "")).strip() or "The model did not provide a reason",
            evidence=[str(item) for item in evidence[:3]],
            model=self.model_name,
            raw_response=content,
        )

    def _judge_batch_uncached(
        self,
        *,
        root_rid: str,
        attack_description: str,
        child_rid: str,
        items: List[Tuple[DataObject, ReadEvent, WriteEvent]],
    ) -> List[TaintDecision]:
        system_prompt = (
            "You are a security provenance analysis assistant. "
            "Your task is to decide which database rows read by a request were last written with tainted data "
            "that may propagate to the final attack request. "
            "Always output valid JSON."
        )

        items_text: List[str] = []
        for i, (data_obj, read_event, write_event) in enumerate(items):
            table_name, pk = data_obj
            items_text.append(
                f"Data row #{i+1}: {table_name}[{pk}]\n"
                f"- read_event_time: {read_event.time.isoformat()}\n"
                f"- candidate_write_event:\n"
                f"  - request_id: {write_event.rid}\n"
                f"  - time: {write_event.time.isoformat()}\n"
                f"  - event: {write_event.event}\n"
                f"  - table: {write_event.tbn}\n"
                f"  - pk: {write_event.pk}\n"
                f"  - row_after_write_json: {json.dumps(write_event.row, ensure_ascii=False, sort_keys=True, default=str)}"
            )

        items_block = "\n\n".join(items_text)

        user_prompt = f"""
Use the context below to decide whether each data row read by the current backtraced request introduced tainted data.

Decision criteria:
1. If the full row contains a malicious command, attack script, dangerous expression, injection payload, or malicious control data that may propagate to a later attack request, set tainted=true.
2. If the row only contains ordinary business fields, benign configuration, no attack-related context, or no evidence of attack propagation, set tainted=false.
3. For delete events, set tainted=false unless the event contains effective content supporting attack propagation.
4. Be conservative and keep only writes that are genuinely likely to be related to attack propagation.

Final attack request: {root_rid}
Attack context: {attack_description}
Current backtraced request: {child_rid}

The request read the latest write events for the following {len(items)} data rows:

{items_block}

For each data row, decide whether it introduced tainted data and output JSON in the following format:
{{
  "results": [
    {{
      "index": 0,
      "tainted": true,
      "confidence": "high",
      "reason": "The written bio field contains an XSS attack script.",
      "evidence": ["The bio field value is '<script>alert(1)</script>'."]
    }}
  ]
}}
Note: the results array length must equal {len(items)}, and index must range from 0 to {len(items) - 1}.
""".strip()

        body = {
            "model": self.model_name,
            "temperature": 0,
            "max_tokens": max(1024, len(items) * 256),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        request = urllib.request.Request(
            self.api_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw_bytes = response.read()
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"DeepSeek API returned HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DeepSeek API request failed: {exc}") from exc

        payload = json.loads(raw_bytes.decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        batch_json = extract_json_object(content)
        results_raw = batch_json.get("results", [])
        if not isinstance(results_raw, list):
            results_raw = []

        decisions: List[TaintDecision] = []
        for i, (data_obj, _read_event, _write_event) in enumerate(items):
            raw = results_raw[i] if i < len(results_raw) else {}
            evidence = raw.get("evidence", [])
            if not isinstance(evidence, list):
                evidence = [str(evidence)]
            decisions.append(TaintDecision(
                is_tainted=bool(raw.get("tainted")),
                confidence=str(raw.get("confidence", "unknown")),
                reason=str(raw.get("reason", "")).strip() or "The model did not provide a reason",
                evidence=[str(item) for item in evidence[:3]],
                model=self.model_name,
                raw_response=content,
            ))
        return decisions
