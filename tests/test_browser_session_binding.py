"""Real Chromium checks for browser target identity and ownership."""

from __future__ import annotations

import pytest

from codey.automation.browser import (
    open_headless_page,
)
from tests.support.real_cdp import chromium_executable
from tools.browser_experiment_binding import BrowserTargetBindingError, assert_bound, bind_page


def test_headless_session_binding_rejects_other_page_and_context() -> None:
    session = open_headless_page(browser_path=chromium_executable())
    other_context = session.browser.new_context()
    other_page = other_context.new_page()
    same_context_page = session.page.context.new_page()
    try:
        binding = bind_page(session, session.page)
        assert assert_bound(session, binding) is session.page
        with pytest.raises(BrowserTargetBindingError, match="does not match"):
            assert_bound(session, binding, same_context_page)
        other_binding = bind_page(session, other_page)
        assert other_binding.context_token != binding.context_token
        with pytest.raises(BrowserTargetBindingError, match="does not match"):
            assert_bound(session, binding, other_page)
    finally:
        same_context_page.close()
        other_context.close()
        session.close()


def test_headless_session_binding_reports_lost_page_without_replacement() -> None:
    session = open_headless_page(browser_path=chromium_executable())
    replacement = session.page.context.new_page()
    try:
        binding = bind_page(session, session.page)
        session.page.close()
        with pytest.raises(BrowserTargetBindingError):
            assert_bound(session, binding, replacement)
        assert replacement.is_closed() is False
    finally:
        replacement.close()
        session.close()


def test_owned_headless_session_closes_its_browser_resources() -> None:
    session = open_headless_page(browser_path=chromium_executable())
    extra_page = session.page.context.new_page()
    browser = session.browser
    session.close()
    assert browser.is_connected() is False
    assert extra_page.is_closed() is True
