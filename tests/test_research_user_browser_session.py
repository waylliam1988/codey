"""Existing-session configuration is explicit and never launches a replacement browser."""
from __future__ import annotations

from codey.research.browser_search import BrowserSearchProvider
from codey.research.web_sources import BrowserSessionConfig, WebSourceProvider


def test_existing_session_mode_is_translated_to_the_single_task_runtime() -> None:
    provider = BrowserSearchProvider(headless=False, isolated=False, launch=False, require_existing_session=True)
    try:
        assert not provider.browser_config.launch
        assert not provider.browser_config.isolated
        assert provider.browser.resource_binding() is None
    finally:
        provider.close()


def test_existing_session_mode_is_explicitly_available_on_web_source_provider() -> None:
    provider = WebSourceProvider(browser_session=BrowserSessionConfig(require_existing=True))
    assert provider.browser_session is not None and provider.browser_session.require_existing
