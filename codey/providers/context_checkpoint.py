"""Pure selection and validation for a replaceable, protocol-valid context view."""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from os.path import commonprefix
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


def _old_outputs(codec: ApiCodec, items: list[dict[str, Any]]) -> Iterator[tuple[dict[str, Any], str, str]]:
    spans = codec.closed_spans(items)
    latest_tool = next(((start, end) for start, end in reversed(spans)
                        if any(item.get("tool_calls") or item.get("type") == "function_call"
                               for item in items[start:end])), None)
    for start, end in spans[:-2]:
        if (start, end) == latest_tool:
            continue
        for item in items[start:end]:
            key = "content" if item.get("role") == "tool" else "output" if item.get("type") == "function_call_output" else ""
            text = item.get(key)
            if isinstance(text, str) and len(text) >= 2400:
                yield item, key, text


def _result_ref(text: str, result_refs: frozenset[str]) -> str | None:
    # The runtime appends its receipt after the observed body. Quoted references
    # in that body must neither replace it nor rescue an unavailable final receipt.
    matches = re.findall(r"^Stored result: ([A-Za-z0-9_.-]+)\r?$", text, re.MULTILINE)
    return matches[-1] if matches and matches[-1] in result_refs else None


def output_reduction_ready(codec: ApiCodec, items: list[dict[str, Any]], result_refs: frozenset[str], *,
                           batch_receipts: bool) -> bool:
    """Treat unbacked old bodies losslessly; batch at least two backed bodies."""
    backed = 0
    for _, _, text in _old_outputs(codec, items):
        if _result_ref(text, result_refs) is None:
            return True
        backed += 1
    return batch_receipts and backed >= 2


def reduce_old_outputs(codec: ApiCodec, items: list[dict[str, Any]], result_refs: frozenset[str]) -> list[dict[str, Any]]:
    """Encode old structure losslessly; only omit bodies backed by a receipt."""
    result = copy.deepcopy(items)
    for item, key, text in _old_outputs(codec, result):
        result_ref = _result_ref(text, result_refs)
        if result_ref:
            item[key] = ("[Stored body omitted from this view]\n" + f"Stored result: {result_ref}\n"
                         + "Read it with read_tool_result. Do not execute it again.")
        else:
            encoded = summary_source(text)
            if not isinstance(encoded, str):
                item[key] = _source_json(encoded)
    return result


def validate_summary(text: str) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > 32000:
        raise ValueError("invalid work state answer")
    return text.strip()


def summary_source(value: Any) -> Any:
    """Encode repeated structure losslessly for model-facing sources and views."""
    if isinstance(value, str):
        if len(value) < 512:
            return value
        lines = value.splitlines(keepends=True)
        parts: list[Any] = []
        pending: list[str] = []
        index = 0
        while index < len(lines):
            end = index + 1
            while end < len(lines) and lines[end] == lines[index]:
                end += 1
            count = end - index
            if count >= 3:
                parts.extend(_factor_lines(pending))
                pending = []
                parts.append({"repeat": count, "text": lines[index]})
            else:
                pending.extend(lines[index:end])
                if len(pending) >= 64:
                    parts.extend(_factor_lines(pending))
                    pending = []
            index = end
        parts.extend(_factor_lines(pending))
        encoded = {"text_encoding": "Concatenate parts. Templates emit prefix+value+suffix for each value; repeats emit text count times.",
                   "parts": parts}
        return encoded if len(_source_json(encoded)) < len(_source_json(value)) * 0.8 else value
    if isinstance(value, list):
        return [summary_source(item) for item in value]
    if isinstance(value, dict):
        return {key: summary_source(item) for key, item in value.items()}
    return value


def _source_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _line_template(lines: list[str]) -> Any:
    if len(lines) < 4:
        return "".join(lines)
    prefix = commonprefix(lines)
    tails = [line[len(prefix):] for line in lines]
    suffix = commonprefix([tail[::-1] for tail in tails])[::-1]
    values = [tail[:-len(suffix)] if suffix else tail for tail in tails]
    template = {"prefix": prefix, "values": values, "suffix": suffix}
    original = "".join(lines)
    return template if len(_source_json(template)) < len(_source_json(original)) else original


def _factor_lines(lines: list[str]) -> list[Any]:
    if not lines:
        return []
    # A final unterminated line must not defeat an otherwise shared suffix.
    whole = [_line_template(lines)]
    split = [_line_template(lines[:-1]), lines[-1]] if len(lines) >= 5 else whole
    candidates: list[list[Any]] = [whole, split]
    if len(lines) >= 8:
        middle = len(lines) // 2
        candidates.append(_factor_lines(lines[:middle]) + _factor_lines(lines[middle:]))
    return min(candidates, key=lambda parts: len(_source_json(parts)))
