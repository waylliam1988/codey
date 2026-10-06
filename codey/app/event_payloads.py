"""Bounded machine projection of published run events (no execution or stores)."""

from __future__ import annotations

from typing import Any, SupportsIndex, SupportsInt, TypeAlias, cast

from codey.agents.shell_approval import shell_command_event_fields
from codey.runtime.observe.events import MAX_EVENT_RESULT_CHARS, MAX_EVENT_TEXT_CHARS, clip_event_text
from codey.utils.refs import strict_exit_code

SCHEMA_VERSION = 1
_INT_INPUT: TypeAlias = str | bytes | bytearray | SupportsInt | SupportsIndex


def _payload_task_start(common: dict[str, object], event: dict[str, Any]) -> dict[str, object]:
    return {
        **common,
        "project": clip_event_text(event.get("project") or "", 500),
        "provider": clip_event_text(event.get("provider") or "", 80),
        "mode": clip_event_text(event.get("mode") or "", 40),
        "max_turns": _int_or_zero(event.get("max_turns")),
    }


def _payload_status(common: dict[str, object], event: dict[str, Any]) -> dict[str, object]:
    return {
        **common,
        "status": clip_event_text(event.get("status") or event.get("text") or "", 80),
    }


def _payload_info(common: dict[str, object], event: dict[str, Any]) -> dict[str, object]:
    return {**common, "text": clip_event_text(event.get("text") or "")}


def _payload_shell_request(common: dict[str, object], event: dict[str, Any]) -> dict[str, object]:
    command_fields = shell_command_event_fields(event)
    payload = {
        **common,
        "id": clip_event_text(event.get("id") or "", 80),
        "cwd": clip_event_text(event.get("cwd") or ".", 240),
        **command_fields,
        "deferred_tool_count": _int_or_zero(event.get("deferred_tool_count")),
    }
    deferred = event.get("deferred_tool_calls")
    if isinstance(deferred, list):
        payload["deferred_tool_calls"] = [
            _bounded_deferred_tool_call(row)
            for row in deferred[:8]
            if isinstance(row, dict)
        ]
    return payload


def _payload_turn(common: dict[str, object], event: dict[str, Any]) -> dict[str, object]:
    payload = {**common, "turn": _int_or_zero(event.get("turn"))}
    if event.get("note"):
        payload["note"] = clip_event_text(event.get("note") or "")
    if isinstance(event.get("reasoning"), str) and event["reasoning"].strip():
        payload["reasoning"] = clip_event_text(event["reasoning"], 64_000)
    return payload


def _payload_tool_started(common: dict[str, object], event: dict[str, Any]) -> dict[str, object]:
    payload = {
        **common,
        "turn": _int_or_zero(event.get("turn")),
        "tool_id": clip_event_text(event.get("tool_id") or "", 40),
        "tool": clip_event_text(event.get("kind") or "", 80),
        "tool_name": clip_event_text(event.get("tool_name") or "", 80),
        "path": clip_event_text(event.get("path") or "", 240),
        "activity": clip_event_text(event.get("activity") or ""),
    }
    command = clip_event_text(event.get("command") or "")
    if command:
        payload["command"] = command
    return payload


def _payload_tool(common: dict[str, object], event: dict[str, Any]) -> dict[str, object]:
    payload = {
        **common,
        "turn": _int_or_zero(event.get("turn")),
        "tool_id": clip_event_text(event.get("tool_id") or "", 40),
        "tool": clip_event_text(event.get("kind") or "", 80),
        "tool_name": clip_event_text(event.get("tool_name") or "", 80),
        "path": clip_event_text(event.get("path") or "", 240),
        "ok": event.get("ok") is True,
        "status": clip_event_text(event.get("status") or "", 80),
        "changed": bool(event.get("changed", False)),
        "truncated": bool(event.get("truncated", False)),
        "result": clip_event_text(event.get("result") or "", MAX_EVENT_RESULT_CHARS),
    }
    command = clip_event_text(event.get("command") or "")
    if command:
        payload["command"] = command
    # Strict exit: only real ints are projected; bool/str/float are omitted
    # instead of being coerced to 0 (bool False must not become exit 0).
    strict_exit = strict_exit_code(event.get("exit_code"))
    if strict_exit is not None:
        payload["exit_code"] = strict_exit
    _copy_if_present(payload, event, "output_handle", limit=120)
    for key in ("output_bytes", "output_stored_bytes"):
        if event.get(key) is not None:
            payload[key] = _int_or_zero(event.get(key))
    _copy_if_present(payload, event, "output_sha256", limit=80)
    return payload


def _payload_task_done(common: dict[str, object], event: dict[str, Any]) -> dict[str, object]:
    payload = {
        **common,
        "summary": clip_event_text(event.get("summary") or ""),
        "stop_reason": clip_event_text(event.get("stop_reason") or "", 80),
        "turns": _int_or_zero(event.get("turns")),
        "max_turns": _int_or_zero(event.get("max_turns")),
        "provider": clip_event_text(event.get("provider") or "", 80),
        "mode": clip_event_text(event.get("mode") or "", 40),
    }
    if event.get("changed") is not None:
        payload["changed"] = bool(event.get("changed"))
    _copy_if_present(payload, event, "ledger_path", limit=500)
    receipt = event.get("receipt")
    if isinstance(receipt, dict):
        payload["receipt"] = bounded_receipt(receipt)
    changes = event.get("changes")
    if isinstance(changes, dict):
        payload["changes"] = _bounded_changes(changes)
    failure = event.get("provider_failure")
    if isinstance(failure, dict):
        payload["provider_failure"] = _bounded_provider_failure(failure)
    review = event.get("review")
    if isinstance(review, dict):
        payload["review"] = _bounded_review(review)
    return payload


def _payload_review(common: dict[str, object], event: dict[str, Any]) -> dict[str, object]:
    payload = {**common, "text": clip_event_text(event.get("text") or "")}
    review = event.get("review")
    if isinstance(review, dict):
        payload["review"] = _bounded_review(review)
    return payload


def _payload_headless_close(common: dict[str, object], event: dict[str, Any]) -> dict[str, object]:
    return {
        **common,
        "stop_reason": clip_event_text(event.get("stop_reason") or "", 80),
        "exit_code": _int_or_zero(event.get("exit_code")),
    }


def machine_event_payload(event: object) -> dict[str, object] | None:
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "")
    common = {
        "schema_version": SCHEMA_VERSION,
        "type": event_type,
    }
    for key in ("run_id", "session_id"):
        value = str(event.get(key) or "")
        if value:
            common[key] = value
    if event_type == "task_start":
        return _payload_task_start(common, event)
    if event_type == "status":
        return _payload_status(common, event)
    if event_type == "info":
        return _payload_info(common, event)
    if event_type == "shell_request":
        return _payload_shell_request(common, event)
    if event_type == "turn":
        return _payload_turn(common, event)
    if event_type == "reasoning":
        return {**common, "turn": _int_or_zero(event.get("turn")),
                "text": clip_event_text(event.get("text") or "", 64_000)}
    if event_type == "tool_started":
        return _payload_tool_started(common, event)
    if event_type == "tool":
        return _payload_tool(common, event)
    if event_type == "task_done":
        return _payload_task_done(common, event)
    if event_type == "review":
        return _payload_review(common, event)
    if event_type == "headless_close":
        return _payload_headless_close(common, event)
    return None


def _int_or_zero(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(cast(_INT_INPUT, value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _copy_if_present(
    target: dict[str, object],
    source: dict[str, Any],
    key: str,
    *,
    limit: int = MAX_EVENT_TEXT_CHARS,
) -> None:
    value = str(source.get(key) or "")
    if value:
        target[key] = clip_event_text(value, limit)


def _receipt_display(receipt: dict[str, Any]) -> dict[str, object]:
    display = receipt.get("display")
    if not isinstance(display, dict):
        return {}
    section: dict[str, object] = {}
    summary = clip_event_text(display.get("summary") or "")
    if summary:
        section["summary"] = summary
    detail = clip_event_text(display.get("detail") or "")
    if detail:
        section["detail"] = detail
    return section


def _receipt_work(receipt: dict[str, Any]) -> dict[str, object]:
    work = receipt.get("work")
    if not isinstance(work, dict):
        return {}
    section: dict[str, object] = {}
    if "changed_count" in work:
        section["changed_count"] = _int_or_zero(work.get("changed_count"))
    mode = clip_event_text(work.get("mode") or "", 40)
    if mode:
        section["mode"] = mode
    if "restore_available" in work:
        section["restore_available"] = bool(work.get("restore_available"))
    return section


def _receipt_verification(receipt: dict[str, Any]) -> dict[str, object]:
    verification = receipt.get("verification")
    if not isinstance(verification, dict):
        return {}
    section: dict[str, object] = {}
    trust = clip_event_text(verification.get("trust") or "", 20)
    if trust:
        section["trust"] = trust
    if type(verification.get("checks_passed")) is bool:
        section["checks_passed"] = verification["checks_passed"]
    state = clip_event_text(verification.get("state") or "", 40)
    if state:
        section["state"] = state
    proof_refs = [
        clip_event_text(ref, 120)
        for ref in (verification.get("proof_refs") or [])
    ]
    bounded_refs = [ref for ref in proof_refs if ref][:2]
    if bounded_refs:
        section["proof_refs"] = bounded_refs
    return section


def _receipt_integrity(receipt: dict[str, Any]) -> dict[str, object]:
    integrity = receipt.get("integrity")
    if not isinstance(integrity, dict):
        return {}
    section: dict[str, object] = {}
    status = clip_event_text(integrity.get("status") or "", 20)
    if status:
        section["status"] = status
    severity = clip_event_text(integrity.get("severity") or "", 20)
    if severity:
        section["severity"] = severity
    reason_codes = [
        clip_event_text(code, 80)
        for code in (integrity.get("reason_codes") or [])
    ]
    bounded_codes = [code for code in reason_codes if code]
    if bounded_codes:
        section["reason_codes"] = bounded_codes
    paths = [
        clip_event_text(path, 240)
        for path in (integrity.get("affected_paths") or [])
    ]
    bounded_paths = [path for path in paths if path][:4]
    if bounded_paths:
        section["affected_paths"] = bounded_paths
    refs = [
        clip_event_text(ref, 80)
        for ref in (integrity.get("refs") or [])
    ]
    bounded_integrity_refs = [ref for ref in refs if ref][:4]
    if bounded_integrity_refs:
        section["refs"] = bounded_integrity_refs
    return section


def bounded_receipt(receipt: dict[str, Any]) -> dict[str, object]:
    """Bounded schema-v1 receipt projection for the JSONL stream."""

    payload: dict[str, object] = {}
    schema = receipt.get("schema_version")
    if type(schema) is int and schema == SCHEMA_VERSION:
        payload["schema_version"] = schema
    display_section = _receipt_display(receipt)
    if display_section:
        payload["display"] = display_section
    work_section = _receipt_work(receipt)
    if work_section:
        payload["work"] = work_section
    verification_section = _receipt_verification(receipt)
    if verification_section:
        payload["verification"] = verification_section
    integrity_section = _receipt_integrity(receipt)
    if integrity_section:
        payload["integrity"] = integrity_section
    return payload


def _bounded_deferred_tool_call(row: dict[str, Any]) -> dict[str, object]:
    payload = {
        "tool_index": _int_or_zero(row.get("tool_index")),
        "tool_name": clip_event_text(row.get("tool_name") or "", 80),
    }
    _copy_if_present(payload, row, "path", limit=240)
    _copy_if_present(payload, row, "command", limit=MAX_EVENT_TEXT_CHARS)
    return payload


def _bounded_changes(changes: dict[str, Any]) -> dict[str, object]:
    files = []
    for item in list(changes.get("files") or [])[:3]:
        if not isinstance(item, dict):
            continue
        files.append({
            "path": clip_event_text(item.get("path") or "", 240),
            "status": clip_event_text(item.get("status") or "", 40),
        })
    return {
        "changed_count": _int_or_zero(changes.get("changed_count")),
        "mode": clip_event_text(changes.get("mode") or "", 40),
        "files": files,
    }


def _bounded_provider_failure(failure: dict[str, Any]) -> dict[str, object]:
    return {
        "kind": clip_event_text(failure.get("kind") or "", 80),
        "action": clip_event_text(failure.get("action") or "", 80),
        "message": clip_event_text(failure.get("message") or ""),
    }


def _bounded_review(review: dict[str, Any]) -> dict[str, object]:
    payload = {
        "verdict": clip_event_text(review.get("verdict") or "", 40),
        "status": clip_event_text(review.get("status") or "", 40),
        "origin": clip_event_text(review.get("origin") or "", 40),
        "finding_count": _int_or_zero(review.get("finding_count")),
        "attempt_id": clip_event_text(review.get("attempt_id") or "", 80),
        "artifact_sha256": clip_event_text(review.get("artifact_sha256") or "", 80),
    }
    source = clip_event_text(review.get("source_review_run_id") or "", 120)
    if source:
        payload["source_review_run_id"] = source
    return payload
