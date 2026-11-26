"""Acceptance of browser captures from the task runtime."""
from __future__ import annotations

from codey.research import source_download
from codey.research.extract import _fetch_page_failure_kind, _usable_fetch_page_text, requires_authentication
from codey.research.source_acquisition import http_failure_code


def _validate_page_status(url: str, response_status: int | None) -> dict[str, object] | None:
    """Reject failed responses before reading the DOM or selecting a PDF download."""
    if response_status is not None and response_status >= 400:
        return source_download._fetch_failure(
            url, f"could not load page: HTTP {response_status}",
            code=http_failure_code(response_status), delivery="confirmed", http_status=response_status,
        )
    return None


def _validate_page_content_type(url: str, content_type: str) -> dict[str, object] | None:
    normalized_type = content_type.lower()
    if normalized_type and not any(kind in normalized_type for kind in ("html", "text", "xml", "json")):
        return source_download._fetch_failure(
            url, f"unsupported content type: {normalized_type}", status="skipped",
            code="unsupported_content", delivery="confirmed",
        )
    return None


def validate_browser_capture(page: dict[str, object]) -> dict[str, object]:
    """Accept worker observations with the same source rules as standalone fetches."""
    url, text = str(page.get("url") or ""), str(page.get("text") or "")
    status = page.get("http_status")
    invalid = (_validate_page_status(url, status if type(status) is int and isinstance(status, int) else None)
               or _validate_page_content_type(url, str(page.get("content_type") or "")))
    if invalid is None and requires_authentication(text, page.get("password_visible") is True):
        invalid = source_download._fetch_failure(url, "login_required: page requires authentication",
                                                code="login_required", delivery="confirmed")
    observed_short_text = page.get("observation") == "current_page" and page.get("render_pending") is not True
    if invalid is None and (page.get("render_pending") is True
                           or not _usable_fetch_page_text(text, allow_short=observed_short_text)):
        reason = "render_timeout" if page.get("render_pending") is True else _fetch_page_failure_kind(text)
        invalid = source_download._fetch_failure(url, reason,
            code="access_restricted" if reason == "page_unavailable" else "page_not_loaded", delivery="confirmed")
    return {**invalid, **({"acquisition": page["acquisition"]} if "acquisition" in page else {})} if invalid is not None else page
