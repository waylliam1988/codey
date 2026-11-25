"""Validated source failures shared by acquisition and runtime event delivery."""
from __future__ import annotations

from typing import Literal

from codey.types.json_value import JsonObject, canonical_json_object, require_json_string

SourceFailureCode = Literal["access_restricted", "login_required", "page_not_loaded", "page_unavailable",
    "render_script_error", "render_request_failed", "request_failed", "execution_failed", "unsupported_content",
    "policy_denied", "invalid_response", "read_range_unavailable", "snapshot_unavailable",
    "source_not_selected", "document_unreadable"]

SOURCE_OBSERVATIONS: dict[SourceFailureCode, str] = {
    "access_restricted": "restricted", "login_required": "restricted", "page_not_loaded": "not_loaded",
    "page_unavailable": "unavailable", "render_script_error": "execution_error",
    "render_request_failed": "execution_error", "request_failed": "not_observed",
    "execution_failed": "execution_error", "unsupported_content": "unavailable",
    "policy_denied": "not_observed", "invalid_response": "unavailable",
    "read_range_unavailable": "content", "snapshot_unavailable": "not_observed",
    "source_not_selected": "not_observed", "document_unreadable": "document",
}


def source_failure_code(value: object) -> SourceFailureCode:
    if isinstance(value, str):
        for code in SOURCE_OBSERVATIONS:
            if code == value:
                return code
    raise ValueError("invalid source failure code")


def failure_event(failure: object, *, run_id: str, session_id: str, call_id: str) -> JsonObject:
    row = canonical_json_object(failure)
    if set(row) != {"url", "detail", "code"}:
        raise ValueError("invalid source failure fields")
    code = source_failure_code(row["code"])
    return {"schema_version": 1, "code": "source." + code, "category": "source", "scope": "tool",
            "phase": "reading" if code in {"read_range_unavailable", "snapshot_unavailable"} else "acquisition",
            "effect": "not_applicable", "run_id": run_id, "session_id": session_id, "call_id": call_id,
            "incident_id": f"{run_id}:{call_id}",
            "resource": {"kind": "url", "path": require_json_string(row["url"], "source failure URL")},
            "diagnostic": require_json_string(row["detail"], "source failure detail")}
