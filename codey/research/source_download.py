"""Bounded HTTP and PDF acquisition under the existing research network policy."""
from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Mapping
from email.message import Message
from typing import cast
from urllib.parse import urlparse
from urllib.response import addinfourl

from codey.policies.network import check_fetch_url
from codey.research.extract import (
    _MAX_PAGE_CHARS,
    _fetch_page_failure_kind,
    _usable_fetch_page_text,
    extract_text,
    extract_title,
)
from codey.research.http_redirects import (
    build_no_redirect_opener,
    response_charset,
)
from codey.research.http_redirects import (
    close_response as _close_response,
)
from codey.research.http_redirects import (
    is_redirect_status as _is_redirect_status,
)
from codey.research.http_redirects import (
    redirect_target as _redirect_target,
)
from codey.research.pdf_extract import PDF_MAX_BYTES
from codey.runtime.core import cancellation

_FETCH_HTTP_TIMEOUT = 12
_FETCH_HTTP_MAX_BYTES = 1024 * 1024
_PDF_DOWNLOAD_TIMEOUT = 20
_PDF_CHUNK_BYTES = 64 * 1024
_PDF_MAX_REDIRECTS = 5


def _is_pdf_response(content_type: str, url: str) -> bool:
    ctype = (content_type or "").lower()
    if "application/pdf" in ctype or "application/x-pdf" in ctype:
        return True
    return _is_pdf_url(url)

def _is_pdf_url(url: str) -> bool:
    path = urlparse(url or "").path.lower()
    return path.endswith(".pdf")

def fetch_http_document(url: str) -> dict[str, object]:
    """Shared bounded HTTP/PDF acquisition for the selected HTTP provider."""
    page = _download_text_fallback(url)
    if page.get("content_kind") == "pdf_download":
        return _download_pdf_streaming(str(page.get("url") or url), mime_type=str(page.get("mime_type") or ""))
    return page


def _download_text_fallback(url: str) -> dict[str, object]:
    current_url = (url or "").strip()
    redirects = 0
    while True:
        cancellation.check()
        reason = check_fetch_url(current_url)
        if reason:
            return _fetch_failure(current_url, f'{reason}')
        request = _text_request(current_url)
        try:
            response = _open_url_no_redirect(request, timeout=_FETCH_HTTP_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if _is_redirect_status(exc.code):
                next_url = _redirect_target(current_url, exc.headers)
                _close_response(exc)
                current_url, error = _checked_redirect(current_url, next_url, redirects, content_kind="page")
                if error is not None:
                    return error
                redirects += 1
                continue
            _close_response(exc)
            return _fetch_failure(current_url, f'HTTP fallback could not load page: HTTP {exc.code}')
        except urllib.error.URLError as exc:
            return _fetch_failure(current_url, f'HTTP fallback could not load page: {exc}')
        except OSError as exc:
            return _fetch_failure(current_url, f'HTTP fallback could not load page: {exc}')
        with response:
            final_url = response.url or current_url
            reason = check_fetch_url(final_url)
            if reason:
                return _fetch_failure(final_url, f'{reason} (after redirect)')
            status = int(response.status or 0)
            if _is_redirect_status(status):
                next_url = _redirect_target(current_url, response.headers)
                current_url, error = _checked_redirect(current_url, next_url, redirects, content_kind="page")
                if error is not None:
                    return error
                redirects += 1
                continue
            headers = response.headers
            if status >= 400:
                return _fetch_failure(final_url, f'HTTP fallback could not load page: HTTP {status}')
            ctype = (headers.get("content-type") or "").lower()
            if _is_pdf_response(ctype, final_url):
                return _pdf_download_sentinel(final_url, mime_type=ctype)
            if ctype and not any(t in ctype for t in ("html", "text", "xml", "json")):
                return _fetch_failure(final_url, f'unsupported content type: {ctype}', status="skipped")
            data = response.read(_FETCH_HTTP_MAX_BYTES + 1)
            truncated = len(data) > _FETCH_HTTP_MAX_BYTES
            if truncated:
                data = data[:_FETCH_HTTP_MAX_BYTES]
            html = data.decode(response_charset(headers), errors="replace")
            text = extract_text(html)
            if not _usable_fetch_page_text(text):
                failure = _fetch_failure(
                    final_url,
                    "HTTP fallback had no usable visible content: " + _fetch_page_failure_kind(text),
                    title=extract_title(html),
                )
                failure["truncated"] = truncated
                return failure
            text_truncated = len(text) > _MAX_PAGE_CHARS
            if text_truncated:
                text = text[:_MAX_PAGE_CHARS]
            return {"status": "ok",
                "url": final_url,
                "title": extract_title(html) or _title_from_url(final_url),
                "text": text,
                "truncated": truncated or text_truncated,
            }

def _content_length(headers: Mapping[str, str] | Message) -> int | None:
    value = headers.get("content-length")
    try:
        return int(str(value or "").strip())
    except ValueError:
        return None

def _download_pdf_streaming(url: str, *, mime_type: str = "") -> dict[str, object]:
    current_url = (url or "").strip()
    redirects = 0
    while True:
        cancellation.check()
        reason = check_fetch_url(current_url)
        if reason:
            return _fetch_failure(current_url, f'{reason}')
        request = _pdf_request(current_url)
        try:
            response = _open_url_no_redirect(request, timeout=_PDF_DOWNLOAD_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if _is_redirect_status(exc.code):
                next_url = _redirect_target(current_url, exc.headers)
                _close_response(exc)
                current_url, error = _checked_redirect(current_url, next_url, redirects)
                if error is not None:
                    return error
                redirects += 1
                continue
            _close_response(exc)
            return _pdf_skipped(
                current_url, mime_type or "application/pdf", f"PDF could not be downloaded: HTTP {exc.code}"
            )
        except urllib.error.URLError as exc:
            return _pdf_skipped(current_url, mime_type or "application/pdf", f"PDF could not be downloaded: {exc}")
        except OSError as exc:
            return _pdf_skipped(current_url, mime_type or "application/pdf", f"PDF could not be downloaded: {exc}")
        with response:
            final_url = response.url or current_url
            reason = check_fetch_url(final_url)
            if reason:
                return _fetch_failure(final_url, f'{reason} (after redirect)')
            status = int(response.status or 0)
            if _is_redirect_status(status):
                next_url = _redirect_target(current_url, response.headers)
                current_url, error = _checked_redirect(current_url, next_url, redirects)
                if error is not None:
                    return error
                redirects += 1
                continue
            headers = response.headers
            ctype = (headers.get("content-type") or mime_type or "application/pdf").lower()
            length = _content_length(headers)
            if length is not None and length > PDF_MAX_BYTES:
                return _pdf_skipped(
                    final_url, ctype, f"PDF is too large to read safely ({length} bytes > {PDF_MAX_BYTES})"
                )
            body = bytearray()
            cancellation.check()
            while True:
                chunk = response.read(_PDF_CHUNK_BYTES)
                cancellation.check()
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > PDF_MAX_BYTES:
                    return _pdf_skipped(final_url, ctype, f"PDF is too large to read safely (> {PDF_MAX_BYTES} bytes)")
            if not _is_pdf_response(ctype, final_url):
                return _fetch_failure(final_url, f'unsupported content type: {ctype}', status="skipped")
            return {"status": "ok",
                "url": final_url,
                "title": _title_from_url(final_url),
                "text": "",
                "content_kind": "pdf",
                "mime_type": ctype or "application/pdf",
                "bytes": bytes(body),
                "truncated": False,
            }

def _pdf_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/pdf,*/*;q=0.8",
            "User-Agent": "Research PDF Reader",
        },
    )

def _text_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml,text/xml,text/plain,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0 Research Reader",
        },
    )

def _open_url_no_redirect(request: urllib.request.Request, *, timeout: int) -> addinfourl:
    opener = build_no_redirect_opener()
    return cast(addinfourl, opener.open(request, timeout=timeout))

def _pdf_download_sentinel(url: str, *, mime_type: str = "") -> dict[str, object]:
    return {"status": "ok",
        "url": url,
        "title": _title_from_url(url),
        "text": "",
        "content_kind": "pdf_download",
        "mime_type": mime_type or "application/pdf",
        "truncated": False,
    }

def _checked_redirect(
    current_url: str,
    next_url: str,
    redirects: int,
    *,
    content_kind: str = "pdf",
) -> tuple[str, dict[str, object] | None]:
    if not next_url:
        return current_url, _redirect_error(current_url, content_kind, "redirect did not include a Location header")
    if redirects >= _PDF_MAX_REDIRECTS:
        return current_url, _redirect_error(current_url, content_kind, "redirect limit exceeded")
    reason = check_fetch_url(next_url)
    if reason:
        return current_url, _fetch_failure(next_url, f'{reason} (after redirect)')
    return next_url, None

def _fetch_failure(url: str, detail: str, *, status: str = "error", title: str = "",
                   content_kind: str = "", mime_type: str = "") -> dict[str, object]:
    prefix = "SKIPPED" if status == "skipped" else "ERROR"
    payload: dict[str, object] = {"status": status, "detail": detail, "url": url, "title": title,
               "text": f"{prefix}: {detail}", "truncated": False}
    if content_kind:
        payload["content_kind"] = content_kind
    if mime_type:
        payload["mime_type"] = mime_type
    return payload

def _pdf_skipped(url: str, mime_type: str, message: str) -> dict[str, object]:
    return _fetch_failure(url, f'{message}', status="skipped", title=_title_from_url(url), content_kind="pdf", mime_type=mime_type or "application/pdf")

def _redirect_error(url: str, content_kind: str, message: str) -> dict[str, object]:
    if (content_kind or "").casefold() == "pdf":
        return _pdf_skipped(url, "application/pdf", "PDF " + message)
    return _fetch_failure(url, f'page {message}', title=_title_from_url(url))

def _title_from_url(href: str) -> str:
    parsed = urlparse(href)
    return parsed.netloc or href
