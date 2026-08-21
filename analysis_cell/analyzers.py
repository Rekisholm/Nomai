from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Sequence

from .models import CellCandidate, TaintDecision


def extract_json_object(text: str) -> dict[str, Any]:
    content = text.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        if len(lines) >= 3:
            content = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("model response JSON top level must be an object")
    return parsed


class HeuristicCellAnalyzer:
    """Offline smoke-test analyzer; it is not used for paper results."""

    model_name = "heuristic-cell"
    markers = (
        "<script",
        "javascript:",
        "${",
        "{{",
        "runtime.exec",
        "wget ",
        "curl ",
        "/bin/sh",
        "bash -c",
        "select ",
        "union ",
        "../",
    )

    def judge_cells(
        self,
        *,
        root_rid: str,
        child_rid: str,
        attack_description: str,
        candidates: Sequence[CellCandidate],
    ) -> list[TaintDecision]:
        decisions = []
        for candidate in candidates:
            text = json.dumps(candidate.value, ensure_ascii=False, default=str).lower()
            evidence = tuple(marker for marker in self.markers if marker in text)
            decisions.append(TaintDecision(
                tainted=bool(evidence),
                confidence="high" if evidence else "medium",
                reason=(
                    "cell value contains attack-payload markers"
                    if evidence
                    else "cell value contains no attack-payload markers recognized by offline rules"
                ),
                evidence=evidence[:3],
                model=self.model_name,
            ))
        return decisions


class DeepSeekCellAnalyzer:
    def __init__(
        self,
        *,
        api_key: str,
        model_name: str,
        api_url: str,
        timeout_seconds: int = 60,
        batch_size: int = 40,
        retries: int = 2,
    ) -> None:
        self.api_key = api_key
        self.model_name = model_name
        self.api_url = api_url
        self.timeout_seconds = timeout_seconds
        self.batch_size = batch_size
        self.retries = retries

    def judge_cells(
        self,
        *,
        root_rid: str,
        child_rid: str,
        attack_description: str,
        candidates: Sequence[CellCandidate],
    ) -> list[TaintDecision]:
        decisions: list[TaintDecision] = []
        for start in range(0, len(candidates), self.batch_size):
            batch = candidates[start:start + self.batch_size]
            decisions.extend(self._judge_batch(
                root_rid=root_rid,
                child_rid=child_rid,
                attack_description=attack_description,
                candidates=batch,
            ))
        return decisions

    def _judge_batch(
        self,
        *,
        root_rid: str,
        child_rid: str,
        attack_description: str,
        candidates: Sequence[CellCandidate],
    ) -> list[TaintDecision]:
        if not candidates:
            return []

        items = []
        for index, candidate in enumerate(candidates):
            table, pk, column = candidate.cell
            items.append({
                "index": index,
                "table": table,
                "pk": pk,
                "column": column,
                "value": candidate.value,
                "read_time": candidate.read_event.time.isoformat(),
                "origin_request_id": candidate.origin_write.rid,
                "origin_time": candidate.origin_write.time.isoformat(),
                "origin_operation": candidate.origin_write.operation,
            })

        system_prompt = (
            "You are a database attack provenance analysis assistant. The decision "
            "unit is strictly one database cell. Do not mark the current cell as "
            "tainted because another field in the same row is suspicious. Output "
            "valid JSON only."
        )
        user_prompt = f"""
Final attack request: {root_rid}
Current backtrace request: {child_rid}
Attack context: {attack_description}

Decide whether each cell value carries an attack payload or directly controls
attack propagation. Ordinary primary keys, timestamps, statuses, counters,
session noise, and harmless business values should be false. The decision must
be based on the current cell's own column and value, without using content from
other cells in the same row.

Cells:
{json.dumps(items, ensure_ascii=False, default=str)}

Output format:
{{
  "results": [
    {{
      "index": 0,
      "tainted": true,
      "confidence": "high",
      "reason": "this cell directly contains a script payload",
      "evidence": ["specific attack marker in value"]
    }}
  ]
}}
results must contain exactly indexes 0 through {len(items) - 1}, each exactly once.
""".strip()
        body = {
            "model": self.model_name,
            "temperature": 0,
            "max_tokens": max(1024, len(items) * 220),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        last_error: Exception | None = None
        for _attempt in range(self.retries + 1):
            try:
                content = self._request(body)
                return self._parse_decisions(content, len(items))
            except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                last_error = exc
        raise RuntimeError(f"DeepSeek cell judgment failed: {last_error}") from last_error

    def _request(self, body: dict[str, Any]) -> str:
        request = urllib.request.Request(
            self.api_url,
            data=json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"DeepSeek API HTTP {exc.code}: {body_text}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DeepSeek API request failed: {exc}") from exc
        return str(payload["choices"][0]["message"]["content"])

    def _parse_decisions(self, content: str, count: int) -> list[TaintDecision]:
        parsed = extract_json_object(content)
        raw_results = parsed.get("results")
        if not isinstance(raw_results, list):
            raise ValueError("response is missing the results array")

        by_index: dict[int, dict[str, Any]] = {}
        for raw in raw_results:
            if not isinstance(raw, dict):
                raise ValueError("results contains a non-object element")
            index = raw.get("index")
            if not isinstance(index, int) or index in by_index:
                raise ValueError(f"invalid or duplicate index: {index!r}")
            by_index[index] = raw
        if set(by_index) != set(range(count)):
            raise ValueError(
                f"results index set is incomplete: expected={list(range(count))}, "
                f"actual={sorted(by_index)}"
            )

        decisions = []
        for index in range(count):
            raw = by_index[index]
            if not isinstance(raw.get("tainted"), bool):
                raise ValueError(f"tainted for index={index} must be a JSON boolean")
            evidence = raw.get("evidence", [])
            if not isinstance(evidence, list):
                evidence = [evidence]
            decisions.append(TaintDecision(
                tainted=bool(raw.get("tainted")),
                confidence=str(raw.get("confidence", "unknown")),
                reason=str(raw.get("reason", "")).strip() or "model gave no reason",
                evidence=tuple(str(item) for item in evidence[:3]),
                model=self.model_name,
                raw_response=content,
            ))
        return decisions
