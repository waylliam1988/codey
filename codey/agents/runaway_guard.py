"""Deterministic runaway guard: AAA repeats plus ABAB/periodic cycles.

Only intercepts provably stuck loops, never task strategy:

* AAA: same tool + same args + same result N times in a row.
* ABAB/ABCABC: a short period tiling the tail twice with identical
  call AND result fingerprints per position, no file changes, no new
  information, and no edit-epoch movement. A single ``search A, read B,
  search A`` lookback (incomplete second cycle) never triggers.

No A/B is needed: these are safety rails, not behavior experiments.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from codey.agents.state import ToolAttemptRecord
from codey.runtime.core.models import ToolCall, ToolResult

DEFAULT_REPEAT_THRESHOLD = 3
DEFAULT_CYCLE_PERIODS = (2, 3, 4)
DEFAULT_CYCLES = 2


def tool_fingerprint(call: ToolCall) -> str:
    try:
        args = json.dumps(call.args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        args = str(call.args)
    digest = hashlib.sha256(f"{call.name}:{args}".encode()).hexdigest()[:16]
    return f"{call.name}:{digest}"


def result_fingerprint(result: ToolResult) -> str:
    text = str(result.model_text or "")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    status = "ok" if "ERROR:" not in text[:7] else "error"
    return f"{status}:{digest}"


def repeated_failure_key(call: ToolCall, result: ToolResult) -> str:
    return f"{tool_fingerprint(call)}|{result_fingerprint(result)}"


def attempt_record(
    call: ToolCall,
    result: ToolResult,
    *,
    turn: int,
    edit_epoch: int = 0,
) -> ToolAttemptRecord:
    text = str(result.model_text or "")
    audit = getattr(result, "audit", {})
    changed = bool(audit.get("changed")) if isinstance(audit, dict) else False
    return ToolAttemptRecord(
        tool=str(call.name or ""),
        call_fp=tool_fingerprint(call),
        result_fp=result_fingerprint(result),
        ok="ERROR:" not in text[:7],
        changed=changed,
        turn=int(turn),
        edit_epoch=int(edit_epoch or 0),
    )


@dataclass(frozen=True)
class RunawayDecision:
    block: bool
    reason: str = ""
    action: str = "remind"


def _as_records(
    history: Sequence[ToolAttemptRecord | tuple[Any, Any]],
) -> list[ToolAttemptRecord]:
    records: list[ToolAttemptRecord] = []
    for item in history:
        if isinstance(item, ToolAttemptRecord):
            records.append(item)
            continue
        call, result = item
        records.append(attempt_record(call, result, turn=0))
    return records


def detect_exact_repeat(
    records: Sequence[ToolAttemptRecord],
    *,
    threshold: int = DEFAULT_REPEAT_THRESHOLD,
) -> RunawayDecision:
    items = list(records)
    if len(items) < threshold or threshold < 2:
        return RunawayDecision(block=False)
    tail = items[-threshold:]
    keys = {(rec.call_fp, rec.result_fp) for rec in tail}
    if len(keys) == 1:
        return RunawayDecision(
            block=True,
            reason=(
                f"Do not repeat the same failing {tail[-1].tool} call with identical arguments. "
                "Use a narrower read/grep offset, inspect the error, or explain the blocker."
            ),
        )
    reads = [
        rec.call_fp
        for rec in tail
        if rec.tool == "read"
    ]
    if len(reads) == threshold and len(set(reads)) == 1:
        return RunawayDecision(
            block=True,
            reason="Do not re-read the same file offset repeatedly. Advance the offset or switch to grep.",
        )
    return RunawayDecision(block=False)


def detect_periodic_cycle(
    records: Sequence[ToolAttemptRecord],
    *,
    min_period: int = 2,
    max_period: int = 4,
    cycles: int = DEFAULT_CYCLES,
) -> RunawayDecision:
    """Catch ABAB, ABCABC, ABCDABCD: period p tiling the tail ``cycles`` times."""
    items = list(records)
    for period in range(max(1, min_period), max_period + 1):
        window = period * max(2, cycles)
        if len(items) < window:
            continue
        tail = items[-window:]
        if any(rec.changed for rec in tail):
            continue
        if len({rec.edit_epoch for rec in tail}) != 1:
            continue
        slices = [tail[i * period:(i + 1) * period] for i in range(max(2, cycles))]
        first = [(rec.call_fp, rec.result_fp) for rec in slices[0]]
        if all([(rec.call_fp, rec.result_fp) for rec in s] == first for s in slices[1:]):
            names = ", ".join(rec.tool for rec in slices[0])
            tools = {rec.tool for rec in tail}
            if "shell" in tools:
                return RunawayDecision(
                    block=True,
                    action="stop",
                    reason=f"Repeated shell cycle ({names}) with no new information; stopping instead of re-requesting approval.",
                )
            if "edit" in tools:
                return RunawayDecision(
                    block=True,
                    action="require_reread",
                    reason=(
                        f"Repeated edit cycle ({names}) with no progress. "
                        "Read the exact current file content or run a check before editing again; "
                        "do not emit another edit blindly."
                    ),
                )
            return RunawayDecision(
                block=True,
                reason=(
                    f"Repeated tool cycle ({names}) with identical results and no progress. "
                    "Change strategy: narrow the query, read a different offset, or explain the blocker."
                ),
            )
    return RunawayDecision(block=False)


def detect_abab_cycle(
    records: Sequence[ToolAttemptRecord],
    *,
    cycles: int = DEFAULT_CYCLES,
) -> RunawayDecision:
    return detect_periodic_cycle(records, min_period=2, max_period=2, cycles=cycles)


def should_block_or_remind(
    history: Sequence[ToolAttemptRecord | tuple[Any, Any]],
    *,
    threshold: int = DEFAULT_REPEAT_THRESHOLD,
) -> RunawayDecision:
    records = _as_records(history)
    exact = detect_exact_repeat(records, threshold=threshold)
    if exact.block:
        return exact
    return detect_periodic_cycle(records)


__all__ = [
    "DEFAULT_CYCLES",
    "DEFAULT_CYCLE_PERIODS",
    "DEFAULT_REPEAT_THRESHOLD",
    "RunawayDecision",
    "attempt_record",
    "detect_abab_cycle",
    "detect_exact_repeat",
    "detect_periodic_cycle",
    "repeated_failure_key",
    "result_fingerprint",
    "should_block_or_remind",
    "tool_fingerprint",
]
