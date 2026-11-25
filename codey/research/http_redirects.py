"""Small HTTP redirect helpers shared by Research fetch paths."""

from __future__ import annotations

import contextlib
import urllib.request
from email.message import Message
from typing import Protocol, runtime_checkable
from urllib.parse import urljoin

from codey.utils.refs import coerce_int

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: object, code: int, msg: str,
                         headers: Message, newurl: str) -> None:
        return None


def build_no_redirect_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(NoRedirectHandler)


def is_redirect_status(status: object) -> bool:
    try:
        return coerce_int(status or 0) in REDIRECT_STATUSES
    except (TypeError, ValueError, OverflowError):
        return False


@runtime_checkable
class _Headers(Protocol):
    def get(self, key: str) -> object: ...


@runtime_checkable
class _Closable(Protocol):
    def close(self) -> object: ...


@runtime_checkable
class _ContentCharset(Protocol):
    def get_content_charset(self) -> str | None: ...


def redirect_target(current_url: str, headers: object) -> str:
    location = (headers.get("location") or headers.get("Location")) if isinstance(headers, _Headers) else None
    return urljoin(current_url, str(location).strip()) if location else ""


def close_response(response: object) -> None:
    if isinstance(response, _Closable):
        with contextlib.suppress(Exception):
            response.close()


def response_charset(headers: object) -> str:
    """Charset of an HTTP response, tolerating varied response objects."""
    charset = headers.get_content_charset() if isinstance(headers, _ContentCharset) else None
    return charset or "utf-8"


__all__ = [
    "NoRedirectHandler",
    "REDIRECT_STATUSES",
    "build_no_redirect_opener",
    "close_response",
    "is_redirect_status",
    "redirect_target",
    "response_charset",
]
