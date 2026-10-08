"""Workflow readiness and page cleanup do not depend on Chromium frame delivery."""
import pytest

from tests.test_ui_workflow import page as workflow_page
from tests.test_ui_workflow import ui_browser as ui_browser


def test_workflow_fixture_restores_state_without_animation_frame_callbacks(ui_browser, monkeypatch):
    browser, url = ui_browser
    created = []
    real_new_page = browser.new_page

    def new_page(**kwargs):
        page = real_new_page(**kwargs)
        page.set_default_timeout(2000)
        page.add_init_script("window.requestAnimationFrame = () => 1;")
        real_wait = page.wait_for_function

        def wait(expression, **options):
            # The first predicate is false, as when the response arrives after
            # waiting starts. Later predicates read the real restored state.
            return real_wait("() => { window.readyPollCount = (window.readyPollCount || 0) + 1; "
                             "return window.readyPollCount > 1 && CodeyUiState.current().active_id === 'a'; }",
                             **options)

        monkeypatch.setattr(page, "wait_for_function", wait)
        created.append(page)
        return page

    monkeypatch.setattr(browser, "new_page", new_page)
    fixture = workflow_page.__wrapped__((browser, url))
    try:
        page = next(fixture)
        assert page.evaluate("CodeyUiState.current().active_id") == "a"
    finally:
        fixture.close()
        for page in created:
            page.close()


def test_workflow_fixture_reports_state_and_closes_page_when_readiness_fails(ui_browser, monkeypatch):
    browser, url = ui_browser
    created = []
    real_new_page = browser.new_page

    def new_page(**kwargs):
        page = real_new_page(**kwargs)
        page.set_default_timeout(500)
        # Inject a failed readiness condition after boot, independent of timing.
        real_wait = page.wait_for_function

        def wait(expression, **options):
            return real_wait("false", timeout=100, polling=10)

        monkeypatch.setattr(page, "wait_for_function", wait)
        created.append(page)
        return page

    monkeypatch.setattr(browser, "new_page", new_page)
    fixture = workflow_page.__wrapped__((browser, url))
    try:
        with pytest.raises(AssertionError, match="UI boot failed:.*state:"):
            next(fixture)
        assert created[0].is_closed()
    finally:
        fixture.close()
        for page in created:
            page.close()
