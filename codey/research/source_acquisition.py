"""Acquisition facts describe delivery and observation; passages alone prove reading."""
from __future__ import annotations

from typing import Literal

from codey.types.json_value import JsonObject, JsonValue, canonical_json_object, require_json_string

SourceFailureCode = Literal["access_restricted", "login_required", "page_not_loaded", "page_unavailable",
    "render_script_error", "render_request_failed", "request_failed", "execution_failed", "unsupported_content",
    "policy_denied", "invalid_response", "read_range_unavailable", "snapshot_unavailable",
    "source_not_selected", "document_unreadable"]
DeliveryState = Literal["not_attempted", "confirmed", "unconfirmed"]

_OBSERVATIONS: dict[SourceFailureCode, str] = {
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
        for code in _OBSERVATIONS:
            if code == value:
                return code
    raise ValueError("invalid source failure code")


def http_failure_code(status: int) -> SourceFailureCode:
    return "access_restricted" if status in {401, 403, 429} else "page_unavailable"


def attempt(backend: str, url: str, page: dict[str, object]) -> JsonObject:
    status = require_json_string(page.get("status"), "acquisition status")
    code = source_failure_code(page.get("failure_code", "request_failed")) if status != "ok" else ""
    observation = ("download" if page.get("content_kind") == "pdf_download" else
                   "document" if page.get("content_kind") == "pdf" else "content")
    return {"backend": backend, "requested_url": url, "final_url": str(page.get("url") or url),
            "status": status, "delivery": str(page.get("delivery") or ("confirmed" if status == "ok" else "unconfirmed")),
            "observation": _OBSERVATIONS[code] if code else observation, "failure_code": code,
            "detail": str(page.get("detail") or "")}


def with_attempt(page: dict[str, object], backend: str, url: str) -> dict[str, object]:
    if "acquisition" in page:
        return page
    return {**page, "acquisition": {"version": 1, "attempts": [attempt(backend, url, page)]}}


def acquisition_observation(value: object) -> JsonObject:
    """Validate the cold-start v1 contract on both live commit and recovery."""
    row = canonical_json_object(value)
    if set(row) != {"version", "attempts"} or type(row["version"]) is not int or row["version"] != 1:
        raise ValueError("invalid source acquisition version or fields")
    attempts = row["attempts"]
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("source acquisition requires attempts")
    for entry in attempts:
        _validate_attempt(entry)
    return row


def _validate_attempt(value: JsonValue) -> None:
    fields = {"backend", "requested_url", "final_url", "status", "delivery", "observation", "failure_code", "detail"}
    if not isinstance(value, dict) or set(value) != fields or any(not isinstance(item, str) for item in value.values()):
        raise ValueError("invalid source acquisition attempt fields")
    if value["backend"] not in {"http", "browser", "connector"} or value["status"] not in {"ok", "error", "skipped"}:
        raise ValueError("invalid source acquisition backend or status")
    if value["delivery"] not in {"not_attempted", "confirmed", "unconfirmed"}:
        raise ValueError("invalid source acquisition delivery")
    code = source_failure_code(value["failure_code"]) if value["status"] != "ok" else ""
    expected = {_OBSERVATIONS[code]} if code else {"content", "document", "download"}
    if value["observation"] not in expected or value["failure_code"] != code:
        raise ValueError("source acquisition observation differs from status")
    if value["status"] == "ok" and value["delivery"] != "confirmed":
        raise ValueError("source content requires confirmed delivery")


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
