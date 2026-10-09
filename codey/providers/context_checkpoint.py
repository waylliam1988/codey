"""Pure selection and validation for a replaceable, protocol-valid context view."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from codey.providers.api_codec import ApiCodec
from codey.providers.context_ledger import digest


@dataclass(frozen=True)
class ContextRange:
    start: int
    end: int
    source_digest: str


def select_range(codec: ApiCodec, items: list[dict[str, Any]],
                 checkpoints: frozenset[str] = frozenset(), *, merge_checkpoints: bool = False,
                 recent_start: int | None = None) -> ContextRange | None:
    spans = codec.closed_spans(items)
    latest_user = max((i for i, item in enumerate(items) if item.get("role") == "user"), default=-1)
    latest_tool = next(((start, end) for start, end in reversed(spans)
                        if any(item.get("tool_calls") or item.get("type") == "function_call"
                               for item in items[start:end])), None)
    # Existing verified segments stay intact; only newly closed work is summarized.
    eligible = [(start, end) for start, end in spans[:-1]
                if not start <= latest_user < end and (start, end) != latest_tool
                and (recent_start is None or end <= recent_start)
                and not any(digest(item) in checkpoints for item in items[start:end])]
    runs: list[tuple[int, int]] = []
    for start, end in eligible:
        if runs and runs[-1][1] == start:
            runs[-1] = (runs[-1][0], end)
        else:
            runs.append((start, end))
    if checkpoints:
        runs = [span for span in runs if len(str(items[span[0]:span[1]])) >= 256]
    if not runs:
        if merge_checkpoints:
            # Only a hard admission failure may merge old state. Re-read a
            # bounded pair of original segments, never repeatedly summarize all history.
            old = [(start, end) for start, end in spans[:-1]
                   if end == start + 1 and digest(items[start]) in checkpoints]
            for first, second in zip(old, old[1:], strict=False):
                if first[1] == second[0]:
                    return ContextRange(first[0], second[1], digest(items[first[0]:second[1]]))
        return None
    start, end = max(runs, key=lambda span: len(str(items[span[0]:span[1]])))
    return ContextRange(start, end, digest(items[start:end]))


def replace_range(items: list[dict[str, Any]], selected: ContextRange,
                  replacement: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if digest(items[selected.start:selected.end]) != selected.source_digest:
        raise ValueError("context source range changed")
    return copy.deepcopy(items[:selected.start] + replacement + items[selected.end:])


def reduce_old_outputs(codec: ApiCodec, items: list[dict[str, Any]], result_refs: frozenset[str]) -> list[dict[str, Any]]:
    """Only evict recoverable old bodies; never invent a result reference."""
    result = copy.deepcopy(items)
    spans = codec.closed_spans(result)
    for start, end in spans[:-2]:
        for item in result[start:end]:
            key = "content" if item.get("role") == "tool" else "output" if item.get("type") == "function_call_output" else ""
            text = item.get(key)
            if not isinstance(text, str) or len(text) < 2400:
                continue
            match = re.search(r"Stored result: ([A-Za-z0-9_.-]+)", text)
            if match and match[1] in result_refs:
                item[key] = (text[:600] + "\n[Stored body omitted from this view]\n" + text[-600:]
                             + f"\nRead existing result with read_tool_result: {match[1]}. Do not execute it again.")
    return result


def validate_summary(text: str) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > 32000:
        raise ValueError("invalid work state answer")
    return text.strip()


def summary_source(value: Any) -> Any:
    """Run-length encode identical adjacent lines, preserving every distinct fact.

    This only changes auxiliary input. The canonical journal stays byte-for-byte
    intact, and counts retain the multiplicity of repeated observations.
    """
    if isinstance(value, str):
        lines = value.splitlines(keepends=True)
        output: list[str] = []
        index = 0
        while index < len(lines):
            end = index + 1
            while end < len(lines) and lines[end] == lines[index]:
                end += 1
            count = end - index
            output.extend(lines[index:end] if count < 3 else
                          [lines[index], f"[The preceding identical line occurs {count} consecutive times.]\n"])
            index = end
        return "".join(output)
    if isinstance(value, list):
        return [summary_source(item) for item in value]
    if isinstance(value, dict):
        return {key: summary_source(item) for key, item in value.items()}
    return value
