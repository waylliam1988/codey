"""HTTPError owns a response stream on failure, just like a normal response."""

from __future__ import annotations

import io
import urllib.error

import pytest

from codey.providers import local_discovery
from codey.research import browser_search, connector_search


@pytest.mark.parametrize("status", [401, 403, 500])
@pytest.mark.parametrize("operation", ["models", "html", "pdf", "connector"])
def test_http_failure_closes_owned_body_and_preserves_verdict(monkeypatch, operation, status):
    url = "https://example.com/article"
    body = io.BytesIO(b"unavailable")
    error = urllib.error.HTTPError(url, status, "unavailable", {}, body)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(browser_search, "check_fetch_url", lambda *args, **kwargs: "")
    monkeypatch.setattr(connector_search, "check_fetch_url", lambda *args, **kwargs: "")
    if operation == "models":
        monkeypatch.setattr(local_discovery.urllib.request, "urlopen", fail)
        endpoint, reason = local_discovery.probe_local_endpoint_detail(url)
        assert endpoint is None
        assert reason == ("auth" if status in {401, 403} else "unreachable")
    elif operation == "connector":
        monkeypatch.setattr(connector_search, "_open_connector_url", fail)
        with pytest.raises(ValueError, match="connector request failed"):
            connector_search._read_url_text(url, timeout=1)
    else:
        monkeypatch.setattr(browser_search, "_open_url_no_redirect", fail)
        result = (browser_search._download_text_fallback(url) if operation == "html"
                  else browser_search._download_pdf_streaming(url))
        assert result["status"] == ("error" if operation == "html" else "skipped")
        assert f"HTTP {status}" in result["detail"]
    # Keep the exception alive: garbage collection cannot manufacture cleanup.
    assert error.fp is body
    assert body.closed
