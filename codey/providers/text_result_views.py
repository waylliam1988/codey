"""Receipt-backed views of explicitly marked text-protocol result spans.

The kernel supplies offsets and digests. Source text cannot opt itself into
reduction by quoting a marker. User instructions outside spans remain intact.
"""

from __future__ import annotations

from typing import Any

from codey.providers.base import ResultMetadata
from codey.providers.context_ledger import digest

TEXT_RESULTS_KEY = "_codey_text_results"


def validate_text_results(text: str, spans: object) -> list[dict[str, Any]]:
    if not isinstance(spans, list):
        raise ValueError("text result spans must be a list")
    validated = []
    previous_end = 0
    for span in spans:
        if not isinstance(span, dict) or set(span) != {"start", "end", "digest", "result_ref", "tool", "ok"}:
            raise ValueError("invalid text result span")
        start, end = span["start"], span["end"]
        if (type(start) is not int or type(end) is not int or not previous_end <= start < end <= len(text)
                or span["digest"] != digest(text[start:end])
                or not isinstance(span["result_ref"], str) or not span["result_ref"]
                or not isinstance(span["tool"], str) or not span["tool"] or type(span["ok"]) is not bool):
            raise ValueError("text result span differs from its frozen output")
        validated.append(dict(span))
        previous_end = end
    return validated


def reducible_text_result_count(item: dict[str, Any], result_refs: frozenset[str]) -> int:
    if item.get("role") != "user" or TEXT_RESULTS_KEY not in item:
        return 0
    text = item.get("content")
    if not isinstance(text, str):
        raise ValueError("text result view requires text content")
    return sum(span["end"] - span["start"] >= 2400 and span["result_ref"] in result_refs
               for span in validate_text_results(text, item[TEXT_RESULTS_KEY]))


def reduce_text_results(item: dict[str, Any], result_refs: frozenset[str]) -> dict[str, Any]:
    if not reducible_text_result_count(item, result_refs):
        return item
    text = item["content"]
    parts = []
    retained = []
    cursor = size = 0
    for span in validate_text_results(text, item[TEXT_RESULTS_KEY]):
        prefix = text[cursor:span["start"]]
        parts.append(prefix)
        size += len(prefix)
        body = text[span["start"]:span["end"]]
        if len(body) >= 2400 and span["result_ref"] in result_refs:
            metadata = ResultMetadata(span["result_ref"], span["tool"], span["ok"])
            body = ("[Stored body omitted from this view]\n" + metadata.render()
                    + "\nRead it with read_tool_result. Do not execute it again.")
        else:
            retained.append({**span, "start": size, "end": size + len(body)})
        parts.append(body)
        size += len(body)
        cursor = span["end"]
    parts.append(text[cursor:])
    result = {**item, "content": "".join(parts)}
    if retained:
        result[TEXT_RESULTS_KEY] = retained
    else:
        result.pop(TEXT_RESULTS_KEY)
    return result
