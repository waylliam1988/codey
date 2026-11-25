"""Acquisition facts describe delivery and observation; passages alone prove reading."""
from __future__ import annotations

from typing import Literal

from codey.types.json_value import JsonObject, JsonValue, canonical_json_object, require_json_string
from codey.types.source_failure import SOURCE_OBSERVATIONS, SourceFailureCode, source_failure_code

DeliveryState = Literal["not_attempted", "confirmed", "unconfirmed"]


def http_failure_code(status: int) -> SourceFailureCode:
    return "access_restricted" if status in {401, 403, 429} else "page_unavailable"


def attempt(backend: str, url: str, page: dict[str, object]) -> JsonObject:
    status = require_json_string(page.get("status"), "acquisition status")
    code = source_failure_code(page.get("failure_code", "request_failed")) if status != "ok" else ""
    observation = ("download" if page.get("content_kind") == "pdf_download" else
                   "document" if page.get("content_kind") == "pdf" else "content")
    return {"backend": backend, "requested_url": url, "final_url": str(page.get("url") or url),
            "status": status, "delivery": str(page.get("delivery") or ("confirmed" if status == "ok" else "unconfirmed")),
            "observation": SOURCE_OBSERVATIONS[code] if code else observation, "failure_code": code,
            "detail": str(page.get("detail") or "")}


def with_attempt(page: dict[str, object], backend: str, url: str) -> dict[str, object]:
    if "acquisition" in page:
        return page
    return {**page, "acquisition": {"version": 1, "attempts": [attempt(backend, url, page)]}}


def combine_acquisitions(*pages: dict[str, object]) -> dict[str, object]:
    """Return the final capture with each preceding backend's actual trace."""
    attempts: list[JsonValue] = []
    for page in pages:
        entries = acquisition_observation(page["acquisition"])["attempts"]
        assert isinstance(entries, list)
        attempts.extend(entries)
    return {**pages[-1], "acquisition": {"version": 1, "attempts": attempts}}


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
    expected = {SOURCE_OBSERVATIONS[code]} if code else {"content", "document", "download"}
    if value["observation"] not in expected or value["failure_code"] != code:
        raise ValueError("source acquisition observation differs from status")
    if value["status"] == "ok" and value["delivery"] != "confirmed":
        raise ValueError("source content requires confirmed delivery")
