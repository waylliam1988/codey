"""Protocol telemetry serialization for the Trace sidecar.

Pure serializers moved verbatim from ``trace.py``: they project the
recorder-owned ``protocol_telemetry`` counters into bounded payloads.
They own no mutable state — the ``record_protocol_*`` methods stay on
the recorder because they update live counters and first-valid-turn
tracking. No raw prompt, reply, or error text has a field here.
"""

from __future__ import annotations

from collections.abc import Mapping

from codey.policies.redaction import looks_sensitive_code
from codey.research.guards import (
    valid_digest_ref,
)
from codey.runs.text_clip import clip_text as _clip
from codey.runs.trace_schema import (
    MAX_PROTOCOL_ERROR_KINDS,
    MAX_PROTOCOL_UNKNOWN_TOOLS,
    MAX_PROTOCOL_VALID_TURNS,
)
from codey.runs.trace_values import (
    _identifier,
    _nonnegative_int,
)


def _protocol_kind_code(value: object) -> str:
    """Accept an error kind only as a clean short identifier; else drop it."""

    text = _clip(value, 40)
    if not text:
        return ""
    lowered = text.lower()
    if any(
        not char.isascii() or not (char.isalnum() or char in "._-")
        for char in lowered
    ):
        return ""
    if looks_sensitive_code(lowered):
        return ""
    return lowered


def _safe_tool_label(value: object) -> str:
    """Keep a tool label only when it is already a short safe identifier."""

    text = _clip(value, 40)
    if not text:
        return ""
    if any(not (char.isalnum() or char in "._-") for char in text):
        return ""
    lowered = text.lower()
    if looks_sensitive_code(lowered):
        return ""
    return lowered


def _bounded_protocol_count_row(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    bounded: dict[str, int] = {}
    for key, raw in value.items():
        code = _protocol_kind_code(key)
        if not code:
            continue
        bounded[code] = min(999, _nonnegative_int(raw))
        if len(bounded) >= MAX_PROTOCOL_ERROR_KINDS:
            break
    return bounded


def _protocol_phase_payload(row: Mapping[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {}
    codec_name = _identifier(row.get("codec_name"), 40)
    if codec_name:
        payload["codec_name"] = codec_name
    model_hash = _clip(row.get("model_tool_contract_hash"), 80)
    if model_hash:
        payload["model_tool_contract_hash"] = model_hash
    runtime_hash = _clip(row.get("runtime_tool_contract_hash"), 80)
    if runtime_hash:
        payload["runtime_tool_contract_hash"] = runtime_hash
    error_counts = _bounded_protocol_count_row(row.get("protocol_error_counts"))
    if error_counts:
        payload["protocol_error_counts"] = error_counts
    repair_counts = _bounded_protocol_count_row(row.get("repair_prompt_counts"))
    if repair_counts:
        payload["repair_prompt_counts"] = repair_counts
    alias_count = min(999, _nonnegative_int(row.get("alias_rewrite_count")))
    if alias_count:
        payload["alias_rewrite_count"] = alias_count
    arg_repair_counts = _bounded_protocol_count_row(row.get("arg_repair_counts"))
    if arg_repair_counts:
        payload["arg_repair_counts"] = arg_repair_counts
    total = _nonnegative_int(row.get("repair_prompt_count"))
    if total:
        payload["repair_prompt_count"] = total
    first = _nonnegative_int(row.get("first_valid_turn"))
    if first:
        payload["first_valid_turn"] = first
    raw_turns = row.get("valid_turns")
    turns = [
        item
        for item in (
            _nonnegative_int(value)
            for value in (raw_turns if isinstance(raw_turns, list) else ())
        )
        if item
    ][:MAX_PROTOCOL_VALID_TURNS]
    if turns:
        payload["valid_turns"] = turns
    raw_tools = row.get("unknown_tools")
    unknown_tools: list[dict[str, object]] = []
    for item in raw_tools if isinstance(raw_tools, list) else ():
        if not isinstance(item, Mapping):
            continue
        digest = valid_digest_ref(item.get("digest"))
        if not digest:
            continue
        entry: dict[str, object] = {
            "digest": digest,
            "count": min(999, _nonnegative_int(item.get("count"))),
        }
        label = _safe_tool_label(item.get("label"))
        if label:
            entry["label"] = label
        unknown_tools.append(entry)
        if len(unknown_tools) >= MAX_PROTOCOL_UNKNOWN_TOOLS:
            break
    if unknown_tools:
        payload["unknown_tools"] = unknown_tools
    if row.get("valid_turns_truncated"):
        payload["valid_turns_truncated"] = True
    if row.get("unknown_tools_truncated"):
        payload["unknown_tools_truncated"] = True
    return payload


def _protocol_telemetry_payload(
    rows: Mapping[str, dict[str, object]],
) -> dict[str, object]:
    phases: dict[str, object] = {}
    for phase, row in rows.items():
        key = _identifier(phase, 40)
        if not key or not isinstance(row, Mapping):
            continue
        phases[key] = _protocol_phase_payload(row)
    return {"phases": phases}
