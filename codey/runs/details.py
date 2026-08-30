"""User-facing bounded summaries for one Codey run.

Run Details is a quiet explanation projection. It reads the durable run ledger
and run trace, but it never returns raw prompts, raw tool output, source bodies,
webpage bodies, or provider error text.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from codey.storage.local_store import read_json
from codey.runtime.effects import (
    PHASE_TERMINAL,
    RuntimeOperationState,
    operation_progress_text,
)
from codey.runs.ledger_projection import RunLedgerProjection, load_run_projection
from codey.runs.receipt import (
    VERIFICATION_TRUST_LIMITED,
    VERIFICATION_TRUST_NEEDS_REVIEW,
    VERIFICATION_TRUST_TRUSTED,
)
from codey.runs.trace import MAX_TRACE_BYTES, SCHEMA_VERSION, TRACE_KIND


MAX_ROW_VALUE_CHARS = 180
MAX_WARNING_CHARS = 120
TRUNCATED_TEXT_SUFFIX = "..."
DETAILS_TITLE = "Run details"
PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "glm": "GLM",
    "local": "Local",
    "mimo": "MiMo",
    "qwen": "Qwen",
    "stepfun": "StepFun",
}
WORK_LABELS = {
    "agent": "Project writing",
    "chat": "Chat",
    "hybrid": "Research and project writing",
    "planning": "Planning",
    "project": "Project writing",
    "research": "Research",
    "review": "Review",
}


@dataclass(frozen=True)
class RunDetailsRow:
    label: str
    value: str
    tone: str = "neutral"

    def to_jsonable(self) -> dict[str, str]:
        tone = self.tone if self.tone in {"neutral", "warning"} else "neutral"
        return {
            "label": _clip(self.label, 40),
            "value": _clip(self.value, MAX_ROW_VALUE_CHARS),
            "tone": tone,
        }


@dataclass(frozen=True)
class RunDetailsSummary:
    available: bool
    title: str = DETAILS_TITLE
    rows: tuple[RunDetailsRow, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_jsonable(self) -> dict[str, object]:
        return {
            "title": _clip(self.title, 80) or DETAILS_TITLE,
            "rows": [row.to_jsonable() for row in self.rows],
            "warnings": [_clip(warning, MAX_WARNING_CHARS) for warning in self.warnings],
        }


def load_run_details(
    *,
    run_ledgers: Any,
    run_traces: Any,
    session_id: str,
    run_id: str,
    runtime_operations: Any = None,
) -> RunDetailsSummary:
    """Build a short UI-ready explanation for one run."""

    session = _safe_key(session_id)
    run = _safe_key(run_id)
    if not session or not run:
        return unavailable_summary()

    operation = _load_operation_state(runtime_operations, session, run)
    projection = load_run_projection(run_ledgers, session, run)
    trace = _load_trace_payload(run_traces, session, run)
    if projection is None and trace is None and operation is None:
        return unavailable_summary()

    rows = _summary_rows(projection, trace or {}, operation)
    warnings = _summary_warnings(projection, trace or {})
    return RunDetailsSummary(
        available=bool(rows),
        rows=tuple(rows),
        warnings=tuple(warnings),
    )


def unavailable_summary() -> RunDetailsSummary:
    return RunDetailsSummary(
        available=False,
        rows=(RunDetailsRow("Status", "Details unavailable", "warning"),),
    )


def _summary_rows(
    projection: RunLedgerProjection | None,
    trace: Mapping[str, object],
    operation: RuntimeOperationState | None = None,
) -> list[RunDetailsRow]:
    rows: list[RunDetailsRow] = []
    work = _work_label(
        _projection_mode(projection)
        or _str(trace.get("mode_final"))
        or _operation_mode(operation)
    )
    if work:
        rows.append(RunDetailsRow("Work", work))

    model = _model_label(
        _projection_model(projection)
        or _str(trace.get("provider_final"))
        or _str(trace.get("provider_initial"))
        or _operation_model(operation)
    )
    if model:
        rows.append(RunDetailsRow("Model", model))

    progress = _operation_progress_row(projection, operation)
    if progress:
        rows.append(RunDetailsRow("Progress", progress, "warning"))

    context = _context_summary(projection, trace)
    if context:
        rows.append(RunDetailsRow("Context", context))

    actions = _actions_summary(projection)
    if actions:
        rows.append(RunDetailsRow("Actions", actions))

    safety = _safety_summary(trace)
    if safety:
        tone = "warning" if "blocked" in safety or "approval" in safety else "neutral"
        rows.append(RunDetailsRow("Safety", safety, tone))

    fallback = _fallback_summary(projection, trace)
    if fallback:
        rows.append(RunDetailsRow("Model fallback", fallback))

    verification, verification_tone = _verification_summary(projection)
    if verification:
        rows.append(RunDetailsRow("Verification", verification, verification_tone))

    return rows


def _load_operation_state(
    runtime_operations: Any,
    session_id: str,
    run_id: str,
) -> RuntimeOperationState | None:
    """Read one run's operation state; anything unreadable stays silent."""

    if runtime_operations is None:
        return None
    try:
        operation = runtime_operations.load(session_id, run_id)
    except Exception:
        return None
    if operation is None or operation.phase == PHASE_TERMINAL:
        return None
    return operation


def _operation_progress_row(
    projection: RunLedgerProjection | None,
    operation: RuntimeOperationState | None,
) -> str:
    # A non-terminal snapshot next to a finished ledger is stale: the run
    # completed, so the interrupted-position line would be a lie.
    if operation is None or (projection is not None and projection.has_run_finished):
        return ""
    return operation_progress_text(operation)


def _summary_warnings(
    projection: RunLedgerProjection | None,
    trace: Mapping[str, object],
) -> list[str]:
    warnings: list[str] = []
    if projection is not None and projection.ledger_truncated:
        warnings.append("Some ledger details were truncated.")
    if _list(trace.get("warnings")):
        warnings.append("Some trace details were truncated.")
    return warnings


def _projection_mode(projection: RunLedgerProjection | None) -> str:
    return projection.mode if projection is not None else ""


def _projection_model(projection: RunLedgerProjection | None) -> str:
    if projection is None:
        return ""
    return projection.provider_final or projection.provider_initial


def _operation_mode(operation: RuntimeOperationState | None) -> str:
    return operation.task_kind if operation is not None else ""


def _operation_model(operation: RuntimeOperationState | None) -> str:
    return operation.provider_id if operation is not None else ""


def _work_label(value: str) -> str:
    text = _identifier(value)
    return WORK_LABELS.get(text, text.replace("_", " ").title() if text else "")


def _model_label(value: str) -> str:
    text = _identifier(value)
    return PROVIDER_LABELS.get(text, text.replace("_", " ").title() if text else "")


def _context_summary(
    projection: RunLedgerProjection | None,
    trace: Mapping[str, object],
) -> str:
    parts: list[str] = []
    if projection is not None and projection.project:
        parts.append("Project context")
    local_refs = len(_list(trace.get("local_context_refs")))
    if local_refs:
        parts.append(_count_text("Local context ref", local_refs))
    notes = len(_list(trace.get("research_note_ids")))
    sources = len(_list(trace.get("research_source_refs")))
    if sources:
        parts.append(_count_text("Research source", sources))
    if notes:
        parts.append(_count_text("Research note", notes))
    if not parts and _list(trace.get("prompt_sections")):
        parts.append("Task context")
    return _join_parts(parts) or "No extra context recorded"


def _actions_summary(projection: RunLedgerProjection | None) -> str:
    if projection is None:
        return ""
    counts = dict(projection.tool_counts or {})
    read_like = sum(max(0, int(counts.get(name, 0))) for name in (
        "grep",
        "ls",
        "read",
        "references",
        "search",
    ))
    edit_like = max(
        projection.final_changes.changed_count if projection.final_changes is not None else 0,
        len(projection.changed_files_observed),
    )
    checks = max(len(projection.verified_commands), int(counts.get("run", 0) or 0))
    other = max(0, projection.tool_calls - read_like - int(counts.get("edit", 0) or 0) - int(counts.get("write", 0) or 0) - int(counts.get("run", 0) or 0))

    parts: list[str] = []
    if read_like:
        parts.append(f"inspected {_plural(read_like, 'item')}")
    if edit_like:
        parts.append(f"edited {_plural(edit_like, 'file')}")
    if checks:
        parts.append(f"ran {_plural(checks, 'check')}")
    if other:
        parts.append(f"used {_plural(other, 'other action')}")
    if not parts and projection.tool_calls:
        parts.append(f"used {_plural(projection.tool_calls, 'action')}")
    return _sentence_join(parts) if parts else "No local actions recorded"


def _safety_summary(trace: Mapping[str, object]) -> str:
    decisions = [
        item for item in _list(trace.get("policy_decisions"))
        if isinstance(item, Mapping)
    ]
    if not decisions:
        return "No actions blocked"
    denied = sum(1 for item in decisions if _identifier(item.get("decision")) == "deny")
    asked = sum(1 for item in decisions if _identifier(item.get("decision")) == "ask_user")
    parts: list[str] = []
    if denied:
        parts.append(f"blocked {_plural(denied, 'action')}")
    if asked:
        parts.append(f"asked for approval {_plural(asked, 'time')}")
    return _sentence_join(parts) if parts else "No actions blocked"


def _fallback_summary(
    projection: RunLedgerProjection | None,
    trace: Mapping[str, object],
) -> str:
    fallbacks = [
        item for item in _list(trace.get("fallbacks"))
        if isinstance(item, Mapping)
    ]
    if fallbacks:
        first = fallbacks[0]
        from_model = _model_label(_str(first.get("from_provider")))
        to_model = _model_label(_str(first.get("to_provider")))
        if len(fallbacks) == 1 and from_model and to_model:
            return f"{from_model} -> {to_model}"
        return f"Used {_plural(len(fallbacks), 'fallback')}"
    if projection is not None and projection.provider_switches:
        switch = projection.provider_switches[0]
        from_model = _model_label(switch.from_provider)
        to_model = _model_label(switch.to_provider)
        if len(projection.provider_switches) == 1 and from_model and to_model:
            return f"{from_model} -> {to_model}"
        return f"Used {_plural(len(projection.provider_switches), 'fallback')}"
    return "None"


def _verification_summary(
    projection: RunLedgerProjection | None,
) -> tuple[str, str]:
    """The Verification row, projected from the receipt contract alone.

    The schema-v1 receipt is the single source for what the green check
    is worth: ``needs_review`` means high-confidence integrity findings,
    and ``limited`` with a passing run means the verification could not
    be vouched for. Runs whose ledger carries no valid receipt -- older
    ledgers included -- get the honest "not recorded" wording; a green
    claim is never reconstructed from legacy facts or from the trace.
    """

    receipt = (
        projection.final_changes.receipt
        if projection is not None and projection.final_changes is not None
        else None
    )
    if receipt is not None:
        if receipt.verification.trust == VERIFICATION_TRUST_NEEDS_REVIEW:
            return "Test changes may have weakened checks", "warning"
        if (
            receipt.verification.trust == VERIFICATION_TRUST_LIMITED
            and receipt.verification.checks_passed
        ):
            return "Verification monitoring incomplete", "warning"
        if receipt.verification.trust == VERIFICATION_TRUST_TRUSTED:
            return "Checks passed", "neutral"
    if projection is not None and projection.final_changes is not None:
        if projection.final_changes.changed_count:
            return "Checks not recorded", "warning"
    if projection is not None and projection.verified_commands:
        return f"Ran {_plural(len(projection.verified_commands), 'check')}", "neutral"
    if projection is not None and projection.stop_reason and projection.stop_reason != "done":
        return _stop_reason_text(projection.stop_reason), "neutral"
    return "", "neutral"


def _stop_reason_text(value: str) -> str:
    reason = _identifier(value)
    if reason == "max_turns":
        return "Stopped at turn limit"
    if reason == "no_progress":
        return "Paused after no progress"
    if reason == "stopped":
        return "Stopped by user"
    if reason == "error":
        return "Stopped with error"
    return reason.replace("_", " ").capitalize() if reason else ""


def _load_trace_payload(store: Any, session_id: str, run_id: str) -> dict[str, object] | None:
    if store is None:
        return None
    try:
        path = store.path_for(session_id, run_id)
    except Exception:
        return None
    payload = read_json(path, max_bytes=MAX_TRACE_BYTES)
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None
    if payload.get("kind") != TRACE_KIND:
        return None
    return payload


def _safe_key(value: object) -> str:
    return _clip(value, 120)


def _clip(value: object, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    if limit <= len(TRUNCATED_TEXT_SUFFIX):
        return text[:limit]
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATED_TEXT_SUFFIX)].rstrip() + TRUNCATED_TEXT_SUFFIX


def _identifier(value: object) -> str:
    return "".join(
        char if char.isalnum() or char in "._:-" else "_"
        for char in _clip(value, 80).lower()
    )


def _str(value: object) -> str:
    return str(value or "")


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _plural(count: int, noun: str) -> str:
    suffix = "" if count == 1 else "s"
    return f"{count} {noun}{suffix}"


def _count_text(noun: str, count: int) -> str:
    return f"{noun}{'' if count == 1 else 's'} ({count})"


def _join_parts(parts: list[str]) -> str:
    return ", ".join(part for part in parts if part)


def _sentence_join(parts: list[str]) -> str:
    if not parts:
        return ""
    return ", ".join(parts[:3])
