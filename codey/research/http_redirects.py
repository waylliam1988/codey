"""Small HTTP redirect helpers shared by Research fetch paths."""

from __future__ import annotations

import contextlib
import urllib.request
from typing import Any
from urllib.parse import urljoin

from codey.utils.refs import coerce_int

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any) -> None:
        return None


def build_no_redirect_opener() -> Any:
    return urllib.request.build_opener(NoRedirectHandler)


def is_redirect_status(status: object) -> bool:
    try:
        return coerce_int(status or 0) in REDIRECT_STATUSES
    except (TypeError, ValueError, OverflowError):
        return False


def redirect_target(current_url: str, headers: Any) -> str:
    try:
        location = headers.get("location") or headers.get("Location")
    except AttributeError:
        location = ""
    return urljoin(current_url, str(location or "").strip()) if location else ""


def close_response(response: Any) -> None:
    with contextlib.suppress(Exception):
        response.close()


def response_charset(headers: Any) -> str:
    """Charset of an HTTP response, tolerating varied response objects."""
    try:
        charset = headers.get_content_charset()
    except AttributeError:
        charset = ""
    return str(charset or "utf-8")


__all__ = [
    "NoRedirectHandler",
    "REDIRECT_STATUSES",
    "build_no_redirect_opener",
    "close_response",
    "is_redirect_status",
    "redirect_target",
    "response_charset",
]
