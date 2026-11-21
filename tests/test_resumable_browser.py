"""Human assistance changes the document inside the original browser target."""
from __future__ import annotations

import pytest

from codey.automation.browser_contracts import RouteDecision
from tests.support.real_cdp import chromium_executable
from tools.browser_experiment_sources import ResumableBrowserSearchProvider


@pytest.mark.parametrize('body', [
    '<main>Verify you are human</main>',
    '{"error":{"message":"您当前请求存在异常，暂时限制本次访问。","code":40362}}',
])
def test_research_provider_retains_challenge_page_and_resumes_in_place(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    provider = ResumableBrowserSearchProvider(headless=True, browser_path=chromium_executable())
    status, current = 403, body
    requests: list[str] = []

    def route(reply: RouteDecision) -> None:
        requests.append(reply.request.url)
        reply.fulfill(status=status, content_type='text/html', body=current)

    monkeypatch.setattr(provider, '_guard_request', route)
    url = 'https://example.com/challenge'
    try:
        failed = provider.fetch(url)
        assert failed['status'] == 'error'
        binding = provider.browser.resource_binding()
        assert binding is not None
        unresolved = provider.resume_fetch()
        assert unresolved['status'] == 'error' and unresolved['failure_code'] == 'access_restricted'
        assert provider.browser.snapshot().ok
        # Markup changes inside HTTP 403 cannot erase that response. A user
        # navigation obtains a successful response in the same original target.
        status, current = 200, '<article>Visible article after verification with current source information.</article>'
        assert provider.browser.navigate(url).ok
        resumed = provider.resume_fetch()
        assert resumed['status'] == 'ok' and 'Visible article after verification' in str(resumed['text'])
        assert provider.browser.resource_binding() == binding
        assert requests == [url, url]
    finally:
        provider.close()


def test_resumable_provider_reports_missing_page_after_close() -> None:
    provider = ResumableBrowserSearchProvider(headless=True, browser_path=chromium_executable())
    provider.close()
    result = provider.resume_fetch()
    assert result['status'] == 'error' and result['failure_code'] == 'snapshot_unavailable'
