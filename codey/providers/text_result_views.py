"""Receipt-backed views of explicitly marked text-protocol result spans.

The kernel supplies offsets and digests. Source text cannot opt itself into
reduction by quoting a marker. User instructions outside spans remain intact.
"""

from __future__ import annotations

from dataclasses import dataclass

from codey.providers.base import ResultMetadata
from codey.providers.context_ledger import digest
from codey.types.json_value import JsonObject, require_json_boolean, require_json_integer, require_json_string

TEXT_RESULTS_KEY = "_codey_text_results"


@dataclass(frozen=True)
class TextResultSpan:
    """Half-open Unicode codepoint offsets into the exact provider prompt."""

    start: int
    end: int
    digest: str
    result_ref: str
    tool: str
    ok: bool

    def __post_init__(self) -> None:
        require_json_integer(self.start, "start")
        require_json_integer(self.end, "end")
        require_json_boolean(self.ok, "ok")
        if not 0 <= self.start < self.end:
            raise ValueError("invalid text result span coordinates")
        for name, value in (("digest", self.digest), ("result_ref", self.result_ref), ("tool", self.tool)):
            if not require_json_string(value, name):
                raise ValueError(f"text result span {name} must not be empty")

    def to_payload(self) -> JsonObject:
        return {"start": self.start, "end": self.end, "digest": self.digest,
                "result_ref": self.result_ref, "tool": self.tool, "ok": self.ok}


def decode_text_result_spans(text: str, spans: object) -> tuple[TextResultSpan, ...]:
    if not isinstance(spans, list):
        raise ValueError("text result spans must be a list")
    validated: list[TextResultSpan] = []
    previous_end = 0
    for span in spans:
        if not isinstance(span, dict) or set(span) != {"start", "end", "digest", "result_ref", "tool", "ok"}:
            raise ValueError("invalid text result span")
        try:
            parsed = TextResultSpan(
                require_json_integer(span["start"], "start"), require_json_integer(span["end"], "end"),
                require_json_string(span["digest"], "digest"), require_json_string(span["result_ref"], "result_ref"),
                require_json_string(span["tool"], "tool"), require_json_boolean(span["ok"], "ok"))
        except ValueError as exc:
            raise ValueError(f"invalid text result span: {exc}") from exc
        if (not previous_end <= parsed.start < parsed.end <= len(text)
                or parsed.digest != digest(text[parsed.start:parsed.end])):
            raise ValueError("text result span differs from its frozen output")
        validated.append(parsed)
        previous_end = parsed.end
    return tuple(validated)


def validate_text_results(text: str, spans: object) -> list[JsonObject]:
    """Legacy wire projection; internal consumers use typed spans."""
    return [span.to_payload() for span in decode_text_result_spans(text, spans)]


def reducible_text_result_count(item: JsonObject, result_refs: frozenset[str]) -> int:
    if item.get("role") != "user" or TEXT_RESULTS_KEY not in item:
        return 0
    text = item.get("content")
    if not isinstance(text, str):
        raise ValueError("text result view requires text content")
    return sum(span.end - span.start >= 2400 and span.result_ref in result_refs
               for span in decode_text_result_spans(text, item[TEXT_RESULTS_KEY]))


def reduce_text_results(item: JsonObject, result_refs: frozenset[str]) -> JsonObject:
    if not reducible_text_result_count(item, result_refs):
        return item
    text = require_json_string(item["content"], "content")
    parts = []
    retained: list[JsonObject] = []
    cursor = size = 0
    for span in decode_text_result_spans(text, item[TEXT_RESULTS_KEY]):
        prefix = text[cursor:span.start]
        parts.append(prefix)
        size += len(prefix)
        body = text[span.start:span.end]
        if len(body) >= 2400 and span.result_ref in result_refs:
            metadata = ResultMetadata(span.result_ref, span.tool, span.ok)
            body = ("[Stored body omitted from this view]\n" + metadata.render()
                    + "\nRead it with read_tool_result. Do not execute it again.")
        else:
            retained.append({**span.to_payload(), "start": size, "end": size + len(body)})
        parts.append(body)
        size += len(body)
        cursor = span.end
    parts.append(text[cursor:])
    result: JsonObject = {**item, "content": "".join(parts)}
    if retained:
        result[TEXT_RESULTS_KEY] = [dict(row) for row in retained]
    else:
        result.pop(TEXT_RESULTS_KEY)
    return result
