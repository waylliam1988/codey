from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from codey import browser


class BrowserProviderWrapperTests(unittest.TestCase):
    def test_deepseek_wrapper_uses_generic_chat_page(self) -> None:
        session = object()
        profile = Path("deepseek-profile")
        with mock.patch.object(browser, "open_chat_page", return_value=session) as opened:
            result = browser.open_deepseek(port=9333, profile=profile)

        self.assertIs(result, session)
        opened.assert_called_once_with(
            browser.DEEPSEEK_URL,
            "chat.deepseek.com",
            port=9333,
            profile=profile,
            open_if_missing=True,
            bring_to_front=True,
        )

    def test_qwen_wrapper_uses_generic_chat_page(self) -> None:
        session = object()
        profile = Path("qwen-profile")
        with mock.patch.object(browser, "open_chat_page", return_value=session) as opened:
            result = browser.open_qwen(port=9444, profile=profile)

        self.assertIs(result, session)
        opened.assert_called_once_with(
            browser.QWEN_URL,
            "chat.qwen.ai",
            port=9444,
            profile=profile,
            open_if_missing=True,
            bring_to_front=True,
        )

    def test_mimo_wrapper_uses_generic_chat_page(self) -> None:
        session = object()
        profile = Path("mimo-profile")
        with mock.patch.object(browser, "open_chat_page", return_value=session) as opened:
            result = browser.open_mimo(port=9555, profile=profile)

        self.assertIs(result, session)
        opened.assert_called_once_with(
            browser.MIMO_URL,
            "aistudio.xiaomimimo.com",
            port=9555,
            profile=profile,
            open_if_missing=True,
            bring_to_front=True,
        )

    def test_open_chat_page_can_attach_without_opening_missing_tab(self) -> None:
        pw = mock.Mock()
        browser_obj = mock.Mock()
        browser_obj.contexts = [mock.Mock(pages=[])]
        pw.chromium.connect_over_cdp.return_value = browser_obj

        with (
            mock.patch.object(browser, "_port_open", return_value=True),
            mock.patch.object(browser, "_start_playwright_with_retry", return_value=pw),
        ):
            with self.assertRaisesRegex(RuntimeError, "no existing provider tab"):
                browser.open_chat_page(
                    "https://example.test/",
                    "example.test",
                    open_if_missing=False,
                )

        browser_obj.contexts[0].new_page.assert_not_called()
        pw.stop.assert_called_once_with()


class PlaywrightStartupTests(unittest.TestCase):
    def test_start_playwright_retries_internal_missing_playwright_error(self) -> None:
        manager = mock.Mock()
        started = object()
        manager.start.side_effect = [
            AttributeError("'PlaywrightContextManager' object has no attribute '_playwright'"),
            started,
        ]

        with (
            mock.patch.object(browser, "sync_playwright", return_value=manager) as sync_playwright,
            mock.patch.object(browser.time, "sleep") as sleep,
        ):
            result = browser._start_playwright_with_retry()

        self.assertIs(result, started)
        self.assertEqual(sync_playwright.call_count, 2)
        sleep.assert_called_once()

    def test_start_playwright_reports_clear_error_after_retry_exhausted(self) -> None:
        manager = mock.Mock()
        manager.start.side_effect = AttributeError(
            "'PlaywrightContextManager' object has no attribute '_playwright'"
        )

        with (
            mock.patch.object(browser, "sync_playwright", return_value=manager),
            mock.patch.object(browser.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Playwright failed to initialize"):
                browser._start_playwright_with_retry()


if __name__ == "__main__":
    unittest.main()
