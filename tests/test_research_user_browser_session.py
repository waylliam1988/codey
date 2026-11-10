from __future__ import annotations

from unittest import mock

import pytest

from codey.automation.browser import Session


def test_existing_session_mode_connects_existing_cdp_and_can_create_task_page(monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.research.browser_search import BrowserSearchProvider

    captured: dict[str, object] = {}
    session = Session(mock.Mock(), mock.Mock(), mock.Mock(), owns_page=True)

    def open_page(*args: object, **kwargs: object) -> Session:
        captured.update(kwargs)
        return session

    monkeypatch.setattr("codey.research.browser_search.open_chat_page", open_page)
    provider = BrowserSearchProvider(
        headless=False,
        isolated=False,
        launch=False,
        require_existing_session=True,
    )
    assert provider._ensure_session_on_browser_thread() is session
    assert provider._page_owned_by_provider_on_browser_thread(session.page)
    assert captured["open_if_missing"] is False
    assert captured["allow_new_page"] is True


def test_existing_session_mode_is_explicitly_available_on_web_source_provider() -> None:
    from codey.research.web_sources import BrowserSessionConfig, WebSourceProvider

    provider = WebSourceProvider(browser_session=BrowserSessionConfig(require_existing=True))
    assert provider.browser_session.require_existing is True
