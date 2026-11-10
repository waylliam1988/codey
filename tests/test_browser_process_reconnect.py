"""Real process restart reconnects exact CDP resources through original receipts."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from playwright.sync_api import Route, sync_playwright

from codey.automation.browser_binding import BrowserBinding, PageBinding, page_binding
from codey.types.json_value import JsonObject
from tests.support.real_cdp import cdp_browser

USER_HTML = """<label>Draft<textarea></textarea></label>
<button onclick="this.textContent='Clicked '+(Number(this.dataset.count || 0)+1);
this.dataset.count=Number(this.dataset.count || 0)+1">Clicked 0</button>"""


def run_process(stage: str, state: Path, port: int, expected_failure: str = "", read_failure: str = "") -> None:
    log = state.parent / f"{stage}-process.log"
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run([sys.executable, "-m", "tests.support.browser_reconnect_process", stage,
            str(state), str(port), "--expected-failure", expected_failure, "--read-failure", read_failure], stdout=output,
            stderr=subprocess.STDOUT, timeout=90)
    assert result.returncode == 0, log.read_text(encoding="utf-8")


@pytest.mark.parametrize("missing", ["", "tab", "browser", "cleanup"])
def test_process_restart_uses_exact_committed_browser_and_page(tmp_path: Path, missing: str) -> None:
    state = tmp_path / "state"
    with cdp_browser(tmp_path / "profile") as port:
        with sync_playwright() as observer:
            browser = observer.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            browser.contexts[0].pages[0].set_content(USER_HTML)
            browser.contexts[0].add_cookies([{"name": "reconnect_login", "value": "fixture-session",
                                            "url": "https://example.com", "httpOnly": True}])
            user_target = page_binding(browser.contexts[0].pages[0], owned=False).target_id
        # Do not leave an unpumped Playwright observer attached during a blocking
        # subprocess wait: Chromium would pause newly created targets for it.
        run_process("initialize", state, port)
        with sync_playwright() as observer:
            browser = observer.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            user_page = next(page for page in browser.contexts[0].pages
                             if page_binding(page, owned=False).target_id == user_target)
            assert user_page.locator("textarea").input_value() == "Unsaved draft survives Codey restart"
            assert user_page.locator("button").inner_text() == "Clicked 1"
            assert len(browser.contexts[0].pages) == 5
            if missing == "browser":
                browser.new_browser_cdp_session().send("Browser.close")
            elif missing == "tab":
                user_page.close()
        if missing == "browser":
            with cdp_browser(tmp_path / "replacement", port=port):
                run_process("recover", state, port, "original browser instance is gone")
            return
        if missing == "tab":
            run_process("recover", state, port, "original tab is gone")
        else:
            run_process("cleanup" if missing == "cleanup" else "recover", state, port)
        with sync_playwright() as observer:
            browser = observer.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            if missing == "tab":
                assert len(browser.contexts[0].pages) == 4
            else:
                user_page = next(page for page in browser.contexts[0].pages
                                 if page_binding(page, owned=False).target_id == user_target)
                assert user_page.locator("button").inner_text() == ("Clicked 1" if missing == "cleanup" else "Clicked 2")
                assert user_page.locator("textarea").input_value() == "Unsaved draft survives Codey restart"
                assert len(browser.contexts[0].pages) == 1  # Four owned tabs closed; borrowed draft kept.
                assert any(cookie["name"] == "reconnect_login" and cookie["value"] == "fixture-session"
                           for cookie in browser.contexts[0].cookies("https://example.com"))


@pytest.mark.parametrize("field,value", [
    ("version", True), ("version", 2), ("port", True), ("port", 65536),
    ("browser_id", ""), ("active_target", "other"),
    ("pages", [{"target_id": "target", "context_id": "", "owned": "yes"}]),
])
def test_resource_binding_rejects_invalid_receipt_metadata(field: str, value: object) -> None:
    payload: dict[str, object] = dict(BrowserBinding(9222, "browser", "target", (PageBinding("target", "", True),)).payload())
    payload[field] = value
    with pytest.raises((ValueError, TypeError)):
        BrowserBinding.read(payload)


def test_resource_binding_roundtrip_and_duplicate_targets() -> None:
    binding = BrowserBinding(9222, "browser", "target", (PageBinding("target", "", False),))
    assert BrowserBinding.read(binding.payload()) == binding
    duplicate: JsonObject = {**binding.payload(), "pages": [binding.pages[0].payload(), binding.pages[0].payload()]}
    with pytest.raises(ValueError, match="active browser target"):
        BrowserBinding.read(duplicate)


@pytest.mark.parametrize("status,body,error", [
    (503, "Service Unavailable", "HTTP 503"),
    (403, "Forbidden", "HTTP 403"),
    (200, "Checking your browser… Please wait while we verify you are human.", "no usable visible content"),
])
def test_reconnected_document_must_pass_original_source_quality_checks(
    tmp_path: Path, status: int, body: str, error: str,
) -> None:
    state = tmp_path / "state"
    with cdp_browser(tmp_path / "profile") as port:
        with sync_playwright() as observer:
            browser = observer.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            user_page = browser.contexts[0].pages[0]
            user_page.set_content(USER_HTML)
            target = page_binding(user_page, owned=False).target_id
        run_process("initialize", state, port)
        with sync_playwright() as observer:
            browser = observer.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = next(page for page in browser.contexts[0].pages
                        if page_binding(page, owned=False).target_id == target)

            def error_document(route: Route) -> None:
                route.fulfill(status=status, content_type="text/html", body=body)

            page.route("**/*", error_document)
            page.goto("https://example.com/reconnect-error")
        run_process("recover", state, port, read_failure=error)
