from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey import browser


class BrowserProviderWrapperTests(unittest.TestCase):
    def test_find_browser_prefers_configured_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            chrome = Path(td) / "chrome.exe"
            chrome.write_text("", encoding="utf-8")

            with mock.patch.dict(
                browser.os.environ,
                {browser.CODEY_BROWSER_PATH_ENV: str(chrome)},
            ):
                found = browser._find_browser()

        self.assertEqual(found.path, chrome)
        self.assertEqual(found.kind, "chrome")
        self.assertEqual(found.profile, browser.CHROME_PROFILE)

    def test_find_browser_prefers_edge_then_chrome_defaults(self) -> None:
        edge = Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
        chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

        def exists(self: Path) -> bool:
            return self == edge or self == chrome

        with (
            mock.patch.dict(browser.os.environ, {}, clear=True),
            mock.patch.object(Path, "is_file", exists),
        ):
            found = browser._find_browser()

        self.assertEqual(found.path, edge)
        self.assertEqual(found.kind, "edge")
        self.assertEqual(found.profile, browser.EDGE_PROFILE)

    def test_find_browser_falls_back_to_chrome_profile(self) -> None:
        chrome = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")

        def exists(self: Path) -> bool:
            return self == chrome

        with (
            mock.patch.dict(browser.os.environ, {}, clear=True),
            mock.patch.object(Path, "is_file", exists),
        ):
            found = browser._find_browser()

        self.assertEqual(found.path, chrome)
        self.assertEqual(found.kind, "chrome")
        self.assertEqual(found.profile, browser.CHROME_PROFILE)

    def test_find_browser_falls_back_to_user_chrome_profile(self) -> None:
        chrome = Path(r"C:\Users\Ada\AppData\Local\Google\Chrome\Application\chrome.exe")

        def exists(self: Path) -> bool:
            return self == chrome

        with (
            mock.patch.dict(browser.os.environ, {"LOCALAPPDATA": r"C:\Users\Ada\AppData\Local"}, clear=True),
            mock.patch.object(Path, "is_file", exists),
        ):
            found = browser._find_browser()

        self.assertEqual(found.path, chrome)
        self.assertEqual(found.kind, "chrome")
        self.assertEqual(found.profile, browser.CHROME_PROFILE)

    def test_find_browser_reports_clear_missing_browser_error(self) -> None:
        with (
            mock.patch.dict(browser.os.environ, {}, clear=True),
            mock.patch.object(Path, "is_file", return_value=False),
        ):
            with self.assertRaisesRegex(FileNotFoundError, "Edge or Google Chrome"):
                browser._find_browser()

    def test_launch_browser_uses_detected_chrome_profile_for_default_profile(self) -> None:
        exe = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        found = browser.BrowserExecutable(exe, "chrome", browser.CHROME_PROFILE)

        with (
            mock.patch.object(browser, "_find_browser", return_value=found),
            mock.patch.object(Path, "mkdir") as mkdir,
            mock.patch.object(browser.subprocess, "Popen") as popen,
        ):
            browser._launch_browser(9333, browser.DEFAULT_PROFILE, "https://chat.qwen.ai/")

        mkdir.assert_called_once_with(parents=True, exist_ok=True)
        args = popen.call_args.args[0]
        self.assertEqual(args[0], str(exe))
        self.assertIn(f"--user-data-dir={browser.CHROME_PROFILE}", args)

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

    def test_glm_wrapper_uses_generic_chat_page(self) -> None:
        session = object()
        profile = Path("glm-profile")
        with mock.patch.object(browser, "open_chat_page", return_value=session) as opened:
            result = browser.open_glm(port=9666, profile=profile)

        self.assertIs(result, session)
        opened.assert_called_once_with(
            browser.GLM_URL,
            "chatglm.cn",
            port=9666,
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
            mock.patch.object(browser, "_ensure_cdp_port", return_value=9222),
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

    def test_detect_open_provider_tabs_uses_cdp_target_urls(self) -> None:
        targets = [
            {"type": "page", "url": "https://chat.deepseek.com/a/chat/s/1"},
            {"type": "page", "url": "https://aistudio.xiaomimimo.com/#/chat/1"},
            {"type": "service_worker", "url": "https://chat.qwen.ai/sw.js"},
            {"type": "page", "url": "https://chatglm.cn/main/alltoolsdetail?lang=zh"},
        ]

        with (
            mock.patch.object(browser, "_candidate_ports", return_value=(9222,)),
            mock.patch.object(browser, "_cdp_available", return_value=True),
            mock.patch.object(browser, "list_cdp_targets", return_value=targets),
        ):
            statuses = browser.detect_open_provider_tabs()

        self.assertEqual(
            statuses,
            {"deepseek": True, "qwen": False, "mimo": True, "glm": True},
        )

    def test_detect_open_provider_tabs_returns_all_false_when_cdp_is_closed(self) -> None:
        with (
            mock.patch.object(browser, "_candidate_ports", return_value=(9222,)),
            mock.patch.object(browser, "_cdp_available", return_value=False),
            mock.patch.object(browser, "list_cdp_targets") as targets,
        ):
            statuses = browser.detect_open_provider_tabs()

        self.assertEqual(
            statuses,
            {"deepseek": False, "qwen": False, "mimo": False, "glm": False},
        )
        targets.assert_not_called()

    def test_candidate_ports_prefers_saved_cdp_port_after_restart(self) -> None:
        with (
            mock.patch.object(browser, "_active_cdp_port", None),
            mock.patch.object(browser, "_load_saved_cdp_port", return_value=9333),
        ):
            ports = browser._candidate_ports(9222)

        self.assertEqual(ports[:2], (9222, 9333))

    def test_remember_cdp_port_persists_for_next_process(self) -> None:
        with (
            mock.patch.object(browser, "_active_cdp_port", None),
            mock.patch.object(browser, "_save_cdp_port") as save,
        ):
            result = browser._remember_cdp_port(9333)

            self.assertEqual(result, 9333)
            self.assertEqual(browser._active_cdp_port, 9333)
        save.assert_called_once_with(9333)

    def test_find_cdp_port_with_target_scans_saved_port(self) -> None:
        def available(port: int) -> bool:
            return port == 9333

        def targets(port: int):
            if port == 9333:
                return [{"type": "page", "url": "https://chat.deepseek.com/a/chat/s/1"}]
            return []

        with (
            mock.patch.object(browser, "_active_cdp_port", None),
            mock.patch.object(browser, "_load_saved_cdp_port", return_value=9333),
            mock.patch.object(browser, "_cdp_available", side_effect=available),
            mock.patch.object(browser, "list_cdp_targets", side_effect=targets),
            mock.patch.object(browser, "_save_cdp_port"),
        ):
            port = browser._find_cdp_port_with_target("chat.deepseek.com", 9222)

        self.assertEqual(port, 9333)

    def test_open_chat_page_reuses_existing_cdp_with_missing_provider_tab(self) -> None:
        ctx = mock.Mock(pages=[])
        new_page = mock.Mock()
        ctx.new_page.return_value = new_page
        browser_obj = mock.Mock(contexts=[ctx])
        pw = mock.Mock()
        pw.chromium.connect_over_cdp.return_value = browser_obj

        with (
            mock.patch.object(browser, "_ensure_cdp_port", return_value=9333) as ensure,
            mock.patch.object(browser, "_start_playwright_with_retry", return_value=pw),
        ):
            session = browser.open_chat_page("https://chat.qwen.ai/", "chat.qwen.ai")

        ensure.assert_called_once()
        pw.chromium.connect_over_cdp.assert_called_once_with("http://127.0.0.1:9333")
        new_page.goto.assert_called_once_with(
            "https://chat.qwen.ai/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        self.assertIs(session.page, new_page)

    def test_ensure_cdp_port_launches_on_free_fallback_when_default_is_busy(self) -> None:
        def port_open(port: int) -> bool:
            return port == 9222

        with (
            mock.patch.object(browser, "_find_cdp_port_with_target", return_value=None),
            mock.patch.object(browser, "_find_existing_cdp_port", return_value=None),
            mock.patch.object(browser, "_load_saved_cdp_port", return_value=None),
            mock.patch.object(browser, "_port_open", side_effect=port_open),
            mock.patch.object(browser, "_launch_browser") as launch,
            mock.patch.object(browser, "_wait_port") as wait_port,
            mock.patch.object(browser, "_save_cdp_port"),
        ):
            port = browser._ensure_cdp_port(
                preferred=9222,
                profile=Path("profile"),
                start_url="https://chat.deepseek.com/",
                url_contains="chat.deepseek.com",
                open_if_missing=True,
            )

        self.assertEqual(port, 9223)
        launch.assert_called_once_with(9223, Path("profile"), "https://chat.deepseek.com/")
        wait_port.assert_called_once_with(9223)

    def test_ensure_cdp_port_reuses_existing_browser_before_launching(self) -> None:
        with (
            mock.patch.object(browser, "_find_cdp_port_with_target", return_value=None),
            mock.patch.object(browser, "_find_existing_cdp_port", return_value=9333),
            mock.patch.object(browser, "_launch_browser") as launch,
        ):
            port = browser._ensure_cdp_port(
                preferred=9222,
                profile=Path("profile"),
                start_url="https://chat.deepseek.com/",
                url_contains="chat.deepseek.com",
                open_if_missing=True,
            )

        self.assertEqual(port, 9333)
        launch.assert_not_called()


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
            mock.patch.object(browser.cancellation, "wait") as sleep,
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
            mock.patch.object(browser.cancellation, "wait"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Playwright failed to initialize"):
                browser._start_playwright_with_retry()

    def test_start_playwright_race_detection_accepts_quoted_attribute(self) -> None:
        exc = AttributeError('"PlaywrightContextManager" object has no attribute "_playwright"')

        self.assertTrue(browser._is_playwright_startup_race(exc))


if __name__ == "__main__":
    unittest.main()
