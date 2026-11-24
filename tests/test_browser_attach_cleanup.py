import threading
from pathlib import Path

import pytest
from playwright.sync_api import Error, sync_playwright

from codey.automation.browser import CdpEndpoint, list_cdp_targets, open_chat_page
from codey.runtime.core import cancellation
from tests.support.real_cdp import cdp_browser


@pytest.mark.parametrize("interrupted", [False, True])
def test_failed_provider_attachment_closes_only_the_new_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted: bool,
) -> None:
    with cdp_browser(tmp_path / "profile") as port:
        def endpoint(**kwargs: object) -> CdpEndpoint:
            return CdpEndpoint(port)

        monkeypatch.setattr("codey.automation.browser._ensure_cdp_endpoint", endpoint)
        with sync_playwright() as observer:
            browser = observer.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            browser.contexts[0].pages[0].set_content("<article>Open user tab</article>")
        original_pages = [row["id"] for row in list_cdp_targets(port) if row.get("type") == "page"]
        assert len(original_pages) == 1

        if interrupted:
            stop = threading.Event()
            check = cancellation.check

            def cancel_after_new_tab() -> None:
                if sum(row.get("type") == "page" for row in list_cdp_targets(port)) > len(original_pages):
                    stop.set()
                check()

            monkeypatch.setattr(cancellation, "check", cancel_after_new_tab)
            with cancellation.scope(stop), pytest.raises(cancellation.TaskCancelled):
                open_chat_page("about:blank", "example.com", fresh_tab=True, bring_to_front=False)
        else:
            with pytest.raises(Error, match="invalid URL"):
                open_chat_page("invalid-url", "example.com", fresh_tab=True, bring_to_front=False)

        with sync_playwright() as observer:
            browser = observer.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            assert any(page.inner_text("body") == "Open user tab" for page in browser.contexts[0].pages)
        assert [row["id"] for row in list_cdp_targets(port) if row.get("type") == "page"] == original_pages
