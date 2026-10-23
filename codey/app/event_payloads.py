"""Bounded machine projection of published run events (no execution or stores)."""

from __future__ import annotations

import math
from typing import Any, SupportsIndex, SupportsInt, TypeAlias, cast

from codey.agents.shell_approval import shell_command_event_fields
from codey.providers.diagnostics import sanitize_failure_facts
from codey.runtime.observe.events import MAX_EVENT_RESULT_CHARS, MAX_EVENT_TEXT_CHARS, clip_event_text
from codey.utils.refs import strict_exit_code

SCHEMA_VERSION = 1
MAX_FINAL_SUMMARY_CHARS = 64_000
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
    payload = {**common, "text": clip_event_text(event.get("text") or "")}
    if type(event.get("seq")) is int:
        payload["seq"] = event["seq"]
    return payload


def _payload_shell_request(common: dict[str, object], event: dict[str, Any]) -> dict[str, object]:
    command_fields = shell_command_event_fields(event)
    payload = {
        **common,
        "id": clip_event_text(event.get("id") or "", 80),
        "cwd": clip_event_text(event.get("cwd") or ".", 240),
        "project": clip_event_text(event.get("project") or "", 4096),
        "project_rule_available": event.get("project_rule_available") is True,
        "tool_name": clip_event_text(event.get("tool_name") or "shell", 80),
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
    failure = event.get("failure")
    if isinstance(failure, dict):
        payload["failure"] = bounded_failure(failure)
    return payload


def _payload_task_done(common: dict[str, object], event: dict[str, Any]) -> dict[str, object]:
    summary = str(event.get("summary") or "")
    payload = {
        **common,
        # A final answer is a delivery, not a progress preview. Keep structured
        # answers intact within the final-answer bound and expose any clipping.
        "summary": clip_event_text(summary, MAX_FINAL_SUMMARY_CHARS),
        "stop_reason": clip_event_text(event.get("stop_reason") or "", 80),
        "turns": _int_or_zero(event.get("turns")),
        "max_turns": _int_or_zero(event.get("max_turns")),
        "provider": clip_event_text(event.get("provider") or "", 80),
        "mode": clip_event_text(event.get("mode") or "", 40),
    }
    if len(summary) > MAX_FINAL_SUMMARY_CHARS:
        payload["summary_truncated"] = True
    if event.get("changed") is not None:
        payload["changed"] = bool(event.get("changed"))
    if event.get("final_delivery") in {"not_required", "success", "failed", "unknown"}:
        payload["final_delivery"] = event["final_delivery"]
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
        payload["failure"] = _structured_provider_failure(failure, event)
    review = event.get("review")
    if isinstance(review, dict):
        payload["review"] = _bounded_review(review)
    failure = event.get("failure")
    if isinstance(failure, dict):
        payload["failure"] = bounded_failure(failure)
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
    common: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "type": event_type,
    }
    for key in ("run_id", "session_id"):
        value = str(event.get(key) or "")
        if value:
            common[key] = value
    revision = event.get("task_revision")
    if type(revision) is int and 0 <= revision <= 2**53 - 1:
        common["task_revision"] = revision
    if isinstance(event.get("task_status"), str):
        common["task_status"] = event["task_status"]
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


def browser_event_payload(event: object) -> dict[str, object] | None:
    """Keep bounded inspection content on the browser's live and replay stream."""
    payload = machine_event_payload(event)
    if payload is None or not isinstance(event, dict):
        return payload
    if event.get("type") == "task_done":
        if type(event.get("elapsed_ms")) is int:
            payload["elapsed_ms"] = max(0, event["elapsed_ms"])
        if isinstance(event.get("research"), dict):
            fields = {"restoreable", "synthesis_id", "notes_created", "notes_updated", "source_urls", "sources_read",
                      "queries", "search_results", "opened_sources", "coverage", "citation_map", "evidence_items",
                      "counterpoints", "quality_warnings", "max_turns_used", "completion_proof", "followup_applied",
                      "followup_rounds", "pipeline_stop_reason", "planner_stop_reason", "fresh_source_count",
                      "new_evidence_count", "final_evidence_count", "attempted_fresh_source_count", "attempted_new_evidence_count"}
            budget = [64_000, 0]
            payload["research"] = _bounded_research_value({key: value for key, value in event["research"].items() if key in fields}, budget)
            if budget[1]:
                payload["research_truncated"] = True
    if event.get("type") not in {"tool", "tool_started"}:
        return payload
    payload["kind"] = clip_event_text(event.get("kind") or "", 80)
    for key in ('call_id', 'parent_call_id'):
        if isinstance(event.get(key), str) and event[key]:
            payload[key] = clip_event_text(event[key], 512)
    if event.get("type") == "tool_started":
        return payload
    detail = event.get("detail")
    if isinstance(detail, str):
        payload["detail"] = clip_event_text(detail, 64_000)
        payload["detail_truncated"] = event.get("detail_truncated") is True or len(detail) > 64_000
    preview = event.get("preview")
    if isinstance(preview, dict):
        bounded: dict[str, object] = {}
        for key in ("additions", "deletions", "line_start", "line_end", "matches"):
            value = preview.get(key)
            if type(value) is int and 0 <= value <= 2**53 - 1:
                bounded[key] = value
        for key in ("diff_truncated", "diff_unavailable", "has_more"):
            value = preview.get(key)
            if type(value) is bool:
                bounded[key] = value
        query = preview.get("query")
        if isinstance(query, str):
            bounded["query"] = clip_event_text(query, 500)
        diff = preview.get("diff")
        if isinstance(diff, str):
            bounded["diff"] = clip_event_text(diff, 64_000)
            if len(diff) > 64_000:
                bounded["diff_truncated"] = True
        payload["preview"] = bounded
    return payload


def _bounded_research_value(value: object, budget: list[int], depth: int = 0) -> object:
    """Bound browser evidence as one observation, without changing stored facts."""
    if budget[0] <= 0 or depth > 6:
        budget[1] = 1
        return None
    budget[0] -= 16
    if isinstance(value, str):
        text = value[:min(2000, max(0, budget[0] // 4))]
        budget[0] -= len(text.encode("utf-8"))
        budget[1] |= len(text) < len(value)
        return text
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:64]:
            if budget[0] <= 0:
                break
            name = str(key)[:80]
            budget[0] -= len(name.encode("utf-8")) + 4
            result[name] = _bounded_research_value(item, budget, depth + 1)
        budget[1] |= len(result) < len(value)
        return result
    if isinstance(value, (list, tuple)):
        rows = []
        for item in value[:64]:
            if budget[0] <= 0:
                break
            rows.append(_bounded_research_value(item, budget, depth + 1))
        budget[1] |= len(rows) < len(value)
        return rows
    return value if value is None or type(value) in {bool, int} or isinstance(value, float) and math.isfinite(value) else None


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
    payload: dict[str, object] = {
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
    payload: dict[str, object] = {
        "kind": clip_event_text(failure.get("kind") or "", 80),
        "action": clip_event_text(failure.get("action") or "", 80),
        "message": clip_event_text(failure.get("message") or ""),
    }
    facts = sanitize_failure_facts(failure.get("facts"))
    if facts:
        payload["facts"] = facts
    return payload


def _structured_provider_failure(failure: dict[str, Any], event: dict[str, Any]) -> dict[str, object]:
    """Expose a bounded, finite action code alongside legacy diagnostics."""
    kind = str(failure.get("kind") or "transient")
    code = {
        "authentication_required": "provider.authentication_failed",
        "challenge_required": "provider.authentication_failed",
        "rate_limited": "provider.rate_limited",
        "submission_uncertain": "provider.connection_failed",
        "response_missing": "provider.connection_failed",
    }.get(kind, "provider.connection_failed")
    facts_value = failure.get("facts")
    facts: dict[str, Any] = {}
    if isinstance(facts_value, dict):
        facts = cast(dict[str, Any], facts_value)
    return bounded_failure({
        "code": code,
        "category": "provider",
        "scope": "request",
        "phase": failure.get("action") or "request",
        "effect": "unknown" if kind in {"submission_uncertain", "response_missing"} else "not_started",
        "incident_id": str(facts.get("upstream_request_id") or "provider-failure"),
        "source_id": str(event.get("provider") or event.get("provider_id") or ""),
        "diagnostic": failure.get("message") or "",
    })


def bounded_failure(failure: dict[str, Any]) -> dict[str, object]:
    """Project a finite failure fact without exposing executable UI actions."""
    allowed_effects = {"not_started", "applied", "unknown", "not_applicable"}
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "code": clip_event_text(failure.get("code") or "unknown.failure", 80),
        "category": clip_event_text(failure.get("category") or "unknown", 40),
        "scope": clip_event_text(failure.get("scope") or "run", 20),
        "phase": clip_event_text(failure.get("phase") or "", 40),
        "effect": clip_event_text(failure.get("effect") or "unknown", 20),
        "incident_id": clip_event_text(failure.get("incident_id") or "", 120),
    }
    if payload["effect"] not in allowed_effects:
        payload["effect"] = "unknown"
    for key in ("session_id", "run_id", "call_id", "execution_id", "source_id"):
        value = clip_event_text(failure.get(key) or "", 120)
        if value:
            payload[key] = value
    resource = failure.get("resource")
    if isinstance(resource, dict):
        clean_resource = {"kind": clip_event_text(resource.get("kind") or "", 40)}
        for key in ("path", "id"):
            value = clip_event_text(resource.get(key) or "", 240)
            if value:
                clean_resource[key] = value
        if clean_resource["kind"]:
            payload["resource"] = clean_resource
    diagnostic = clip_event_text(failure.get("diagnostic") or "", 1000)
    if diagnostic:
        payload["diagnostic"] = diagnostic
    retry_after = failure.get("retry_after")
    if type(retry_after) is int and 0 <= retry_after <= 86400:
        payload["retry_after"] = retry_after
    return payload


def _bounded_review(review: dict[str, Any]) -> dict[str, object]:
    payload: dict[str, object] = {
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
