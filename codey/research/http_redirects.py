"""Small HTTP redirect helpers shared by Research fetch paths."""

from __future__ import annotations

from urllib.parse import urljoin
import urllib.request

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_no_redirect_opener():
    return urllib.request.build_opener(NoRedirectHandler)


def is_redirect_status(status: object) -> bool:
    try:
        return int(status or 0) in REDIRECT_STATUSES
    except (TypeError, ValueError):
        return False


def redirect_target(current_url: str, headers) -> str:
    try:
        location = headers.get("location") or headers.get("Location")
    except AttributeError:
        location = ""
    return urljoin(current_url, str(location or "").strip()) if location else ""


def close_response(response) -> None:
    try:
        response.close()
    except Exception:
        pass


__all__ = [
    "NoRedirectHandler",
    "REDIRECT_STATUSES",
    "build_no_redirect_opener",
    "close_response",
    "is_redirect_status",
    "redirect_target",
]
