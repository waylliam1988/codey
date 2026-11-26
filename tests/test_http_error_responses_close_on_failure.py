"""HTTPError owns a response stream on failure, just like a normal response."""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from email.message import Message
from typing import NoReturn

import pytest

from codey.providers import local_discovery
from codey.research import connector_search, source_download


@pytest.mark.parametrize("status", [401, 403, 500])
@pytest.mark.parametrize("operation", ["models", "html", "pdf", "connector"])
def test_http_failure_closes_owned_body_and_preserves_verdict(
    monkeypatch: pytest.MonkeyPatch, operation: str, status: int,
) -> None:
    url = "https://example.com/article"
    body = io.BytesIO(b"unavailable")
    error = urllib.error.HTTPError(url, status, "unavailable", Message(), body)

    def fail(*args: object, **kwargs: object) -> NoReturn:
        raise error

    def allow_url(_url: str, **kwargs: object) -> str:
        return ""

    monkeypatch.setattr(source_download, "check_fetch_url", allow_url)
    monkeypatch.setattr(connector_search, "check_fetch_url", allow_url)
    if operation == "models":
        monkeypatch.setattr(urllib.request, "urlopen", fail)
        endpoint, reason = local_discovery.probe_local_endpoint_detail(url)
        assert endpoint is None
        assert reason == ("auth" if status in {401, 403} else "unreachable")
    elif operation == "connector":
        monkeypatch.setattr(connector_search, "_open_connector_url", fail)
        with pytest.raises(ValueError, match="connector request failed"):
            connector_search._read_url_text(url, timeout=1)
    else:
        monkeypatch.setattr(source_download, "_open_url_no_redirect", fail)
        result = (source_download._download_text_fallback(url) if operation == "html"
                  else source_download._download_pdf_streaming(url))
        assert result["status"] == ("error" if operation == "html" else "skipped")
        detail = result["detail"]
        assert isinstance(detail, str) and f"HTTP {status}" in detail
    # Keep the exception alive: garbage collection cannot manufacture cleanup.
    assert error.fp is body
    assert body.closed
