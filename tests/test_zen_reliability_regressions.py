"""Deterministic Zen retry and catalog race regressions."""

import threading

from codey.providers.api_transport import GenerationRejectedError
from codey.providers.zen.catalog import ZenCatalog
from codey.providers.zen.reliability import classify_rejection


def test_quota_error_wins_over_rate_limit_wording():
    error = GenerationRejectedError(429, '{"error":{"type":"RateLimitError","code":"quota_exceeded"}}')
    result = classify_rejection(error)
    assert result.retryable is False and result.kind == "quota"


def test_catalog_late_refresh_cannot_overwrite_newer_snapshot(tmp_path):
    started = threading.Event()
    release_old = threading.Event()
    calls = []

    def fetch(url, etag):
        if url.endswith("/models"):
            return {"data": [{"id": "m", "owned_by": "x"}]}, ""
        calls.append(etag)
        if not started.is_set():
            started.set()
            release_old.wait(1)
            return {"opencode": {"api": "https://opencode.ai/zen/v1", "models": {}}}, "old"
        return {"opencode": {"api": "https://opencode.ai/zen/v1", "models": {}}}, "new"

    catalog = ZenCatalog(tmp_path, fetch=fetch)
    first = threading.Thread(target=lambda: catalog.refresh(force=True))
    first.start()
    assert started.wait(1)
    catalog.refresh(force=True)
    release_old.set()
    first.join(1)
    assert catalog.etag == "new"
