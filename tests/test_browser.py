from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.automation import browser


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

    def test_launch_browser_honors_explicit_browser_path(self) -> None:
        exe = Path(r"C:\Tools\Chromium\chrome.exe")

        def is_file(self: Path) -> bool:
            return self == exe

        with (
            mock.patch.object(browser, "_find_browser", side_effect=AssertionError("should not auto-detect")),
            mock.patch.object(Path, "is_file", is_file),
            mock.patch.object(Path, "mkdir"),
            mock.patch.object(browser.subprocess, "Popen") as popen,
        ):
            browser._launch_browser(
                9333,
                browser.DEFAULT_PROFILE,
                "about:blank",
                browser_path=exe,
            )

        args = popen.call_args.args[0]
        self.assertEqual(args[0], str(exe))

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
            isolated=False,
            fresh_tab=False,
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
            isolated=False,
            fresh_tab=False,
        )

    def test_stepfun_wrapper_uses_generic_chat_page(self) -> None:
        session = object()
        profile = Path("stepfun-profile")
        with mock.patch.object(browser, "open_chat_page", return_value=session) as opened:
            result = browser.open_stepfun(port=9555, profile=profile)

        self.assertIs(result, session)
        opened.assert_called_once_with(
            browser.STEPFUN_URL,
            "chat.stepfun.com",
            port=9555,
            profile=profile,
            open_if_missing=True,
            bring_to_front=True,
            isolated=False,
            fresh_tab=False,
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
            isolated=False,
            fresh_tab=False,
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
            isolated=False,
            fresh_tab=False,
        )

    def test_research_pdf_request_user_agent_does_not_name_product(self) -> None:
        from codey.research import browser_search

        request = browser_search._pdf_request("https://example.com/paper.pdf")

        user_agent = request.headers["User-agent"]
        self.assertEqual(user_agent, "Research PDF Reader")
        self.assertNotIn("Codey", user_agent)

    def test_open_chat_page_can_attach_without_opening_missing_tab(self) -> None:
        pw = mock.Mock()
        browser_obj = mock.Mock()
        browser_obj.contexts = [mock.Mock(pages=[])]
        pw.chromium.connect_over_cdp.return_value = browser_obj

        with (
            mock.patch.object(browser, "_ensure_cdp_endpoint", return_value=browser.CdpEndpoint(9222)),
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

    def test_open_chat_page_uses_bounded_cdp_wait_for_cold_launch(self) -> None:
        ctx = mock.Mock(pages=[mock.Mock(url="https://chatglm.cn/main")])
        browser_obj = mock.Mock(contexts=[ctx])
        pw = mock.Mock()
        pw.chromium.connect_over_cdp.return_value = browser_obj

        with (
            mock.patch.object(
                browser,
                "_ensure_cdp_endpoint",
                return_value=browser.CdpEndpoint(9222),
            ) as ensure,
            mock.patch.object(browser, "_start_playwright_with_retry", return_value=pw),
        ):
            session = browser.open_chat_page(
                "https://chatglm.cn/",
                "chatglm.cn",
                port=9222,
                open_if_missing=True,
            )

        self.assertIs(session.page, ctx.pages[0])
        self.assertEqual(ensure.call_args.kwargs["wait_timeout"], browser.CDP_PORT_WAIT_TIMEOUT)
        self.assertEqual(browser.CDP_PORT_WAIT_TIMEOUT, 20.0)

    def test_detect_open_provider_tabs_uses_cdp_target_urls(self) -> None:
        targets = [
            {"type": "page", "url": "https://chat.deepseek.com/a/chat/s/1"},
            {"type": "page", "url": "https://aistudio.xiaomimimo.com/#/c"},
            {"type": "page", "url": "https://chat.stepfun.com/chats/1"},
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
            {"deepseek": True, "qwen": False, "mimo": True, "stepfun": True, "glm": True},
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
            {"deepseek": False, "qwen": False, "mimo": False, "stepfun": False, "glm": False},
        )
        targets.assert_not_called()

    def test_candidate_ports_prefers_saved_cdp_port_after_restart(self) -> None:
        with (
            mock.patch.object(browser, "_active_cdp_port", None),
            mock.patch.object(browser, "_load_saved_cdp_port", return_value=9333),
        ):
            ports = browser._candidate_ports(9222)

        self.assertEqual(ports[:2], (9222, 9333))

    def test_candidate_ports_scopes_custom_preferred_to_its_port_family(self) -> None:
        with (
            mock.patch.object(browser, "_active_cdp_port", 9222),
            mock.patch.object(browser, "_load_saved_cdp_port", return_value=9223),
        ):
            ports = browser._candidate_ports(9262)

        self.assertEqual(ports, tuple(range(9262, 9271)))

    def test_candidate_ports_keeps_remembered_custom_family_port(self) -> None:
        with (
            mock.patch.object(browser, "_active_cdp_port", 9265),
            mock.patch.object(browser, "_load_saved_cdp_port", return_value=9222),
        ):
            ports = browser._candidate_ports(9262)

        self.assertEqual(ports[:2], (9262, 9265))
        self.assertNotIn(9222, ports)

    def test_remember_cdp_port_persists_for_next_process(self) -> None:
        with (
            mock.patch.object(browser, "_active_cdp_port", None),
            mock.patch.object(browser, "_save_cdp_port") as save,
        ):
            result = browser._remember_cdp_port(9333)

            self.assertEqual(result, 9333)
            self.assertEqual(browser._active_cdp_port, 9333)
        save.assert_called_once_with(9333)

    def test_find_remembered_cdp_port_ignores_default_port_for_custom_preferred(self) -> None:
        with (
            mock.patch.object(browser, "_active_cdp_port", 9222),
            mock.patch.object(browser, "_load_saved_cdp_port", return_value=None),
            mock.patch.object(browser, "_cdp_available") as available,
        ):
            port = browser._find_remembered_cdp_port(9444)

        self.assertIsNone(port)
        available.assert_not_called()

    def test_find_remembered_cdp_port_accepts_custom_preferred_family(self) -> None:
        with (
            mock.patch.object(browser, "_active_cdp_port", 9445),
            mock.patch.object(browser, "_load_saved_cdp_port", return_value=None),
            mock.patch.object(browser, "_cdp_available", return_value=True) as available,
            mock.patch.object(browser, "_save_cdp_port") as save,
        ):
            port = browser._find_remembered_cdp_port(9444)

        self.assertEqual(port, 9445)
        available.assert_called_once_with(9445)
        save.assert_called_once_with(9445)

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
            mock.patch.object(
                browser,
                "_ensure_cdp_endpoint",
                return_value=browser.CdpEndpoint(9333),
            ) as ensure,
            mock.patch.object(browser, "_start_playwright_with_retry", return_value=pw),
        ):
            session = browser.open_chat_page("https://chat.qwen.ai/", "chat.qwen.ai")

        ensure.assert_called_once()
        pw.chromium.connect_over_cdp.assert_called_once_with(
            "http://127.0.0.1:9333",
            timeout=browser.CDP_CONNECT_TIMEOUT_MS,
        )
        new_page.goto.assert_called_once_with(
            "https://chat.qwen.ai/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        self.assertIs(session.page, new_page)

    def test_open_chat_page_does_not_switch_ports_after_attach_failure(self) -> None:
        pw = mock.Mock()
        pw.chromium.connect_over_cdp.side_effect = TimeoutError("attach hung")

        with (
            mock.patch.object(
                browser,
                "_ensure_cdp_endpoint",
                return_value=browser.CdpEndpoint(9222),
            ) as ensure,
            mock.patch.object(browser, "_start_playwright_with_retry", return_value=pw),
        ):
            with self.assertRaisesRegex(TimeoutError, "attach hung"):
                browser.open_chat_page("https://chat.qwen.ai/", "chat.qwen.ai")

        ensure.assert_called_once()
        pw.stop.assert_called_once_with()
        pw.chromium.connect_over_cdp.assert_called_once_with(
            "http://127.0.0.1:9222",
            timeout=browser.CDP_CONNECT_TIMEOUT_MS,
        )

    def test_fresh_tab_opens_new_page_even_when_provider_tab_exists_and_closes_it(self) -> None:
        existing = mock.Mock(url="https://chat.qwen.ai/existing")
        ctx = mock.Mock(pages=[existing])
        new_page = mock.Mock()
        ctx.new_page.return_value = new_page
        browser_obj = mock.Mock(contexts=[ctx])
        pw = mock.Mock()
        pw.chromium.connect_over_cdp.return_value = browser_obj

        with (
            mock.patch.object(
                browser,
                "_ensure_cdp_endpoint",
                return_value=browser.CdpEndpoint(9333),
            ) as ensure,
            mock.patch.object(browser, "_start_playwright_with_retry", return_value=pw),
        ):
            session = browser.open_chat_page(
                "https://chat.qwen.ai/",
                "chat.qwen.ai",
                fresh_tab=True,
                bring_to_front=False,
            )

        ensure.assert_called_once()
        pw.chromium.connect_over_cdp.assert_called_once_with(
            "http://127.0.0.1:9333",
            timeout=browser.CDP_CONNECT_TIMEOUT_MS,
        )
        self.assertIs(session.page, new_page)
        self.assertTrue(session.close_page_on_close)
        new_page.goto.assert_called_once_with(
            "https://chat.qwen.ai/",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        session.close()

        new_page.close.assert_called_once()
        existing.close.assert_not_called()
        pw.stop.assert_called_once()

    def test_fresh_tab_requires_permission_to_open_pages(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "fresh provider tabs require"):
            browser.open_chat_page(
                "https://chat.qwen.ai/",
                "chat.qwen.ai",
                fresh_tab=True,
                open_if_missing=False,
            )

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
        wait_port.assert_called_once_with(9223, timeout=20.0)

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

    def test_isolated_cdp_port_does_not_reuse_existing_browser_or_save_port(self) -> None:
        with (
            mock.patch.object(browser, "_find_cdp_port_with_target") as find_target,
            mock.patch.object(browser, "_find_existing_cdp_port") as find_existing,
            mock.patch.object(browser, "_find_free_isolated_cdp_port", return_value=9444),
            mock.patch.object(browser, "_launch_browser") as launch,
            mock.patch.object(browser, "_wait_port") as wait_port,
            mock.patch.object(browser, "_save_cdp_port") as save,
        ):
            port = browser._ensure_cdp_port(
                preferred=9222,
                profile=Path("worker-profile"),
                start_url="https://chat.qwen.ai/",
                url_contains="chat.qwen.ai",
                open_if_missing=True,
                isolated=True,
            )

        self.assertEqual(port, 9444)
        find_target.assert_not_called()
        find_existing.assert_not_called()
        launch.assert_called_once_with(
            9444,
            Path("worker-profile") / "isolated-9444",
            "https://chat.qwen.ai/",
        )
        wait_port.assert_called_once_with(9444)
        save.assert_not_called()

    def test_isolated_free_port_ignores_stale_remembered_ports(self) -> None:
        def port_open(port: int) -> bool:
            return port == 9444

        with (
            mock.patch.object(browser, "_active_cdp_port", 9222),
            mock.patch.object(browser, "_load_saved_cdp_port", return_value=9222),
            mock.patch.object(browser, "_port_open", side_effect=port_open),
        ):
            port = browser._find_free_isolated_cdp_port(9444)

        self.assertEqual(port, 9445)

    def test_isolated_free_port_for_custom_preferred_does_not_scan_default_family(self) -> None:
        checked_ports: list[int] = []

        def port_open(port: int) -> bool:
            checked_ports.append(port)
            return True

        with mock.patch.object(browser, "_port_open", side_effect=port_open):
            with self.assertRaisesRegex(RuntimeError, "no free isolated CDP port"):
                browser._find_free_isolated_cdp_port(9444)

        self.assertEqual(checked_ports, list(range(9444, 9453)))

    def test_isolated_open_chat_page_closes_launched_browser_process(self) -> None:
        pw = mock.Mock()
        browser_obj = mock.Mock()
        page = mock.Mock(url="https://chat.qwen.ai/")
        browser_obj.contexts = [mock.Mock(pages=[page])]
        pw.chromium.connect_over_cdp.return_value = browser_obj
        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch.object(browser, "_find_free_isolated_cdp_port", return_value=9444),
            mock.patch.object(browser, "_launch_browser", return_value=process),
            mock.patch.object(browser, "_wait_port"),
            mock.patch.object(browser, "_start_playwright_with_retry", return_value=pw),
        ):
            session = browser.open_chat_page(
                "https://chat.qwen.ai/",
                "chat.qwen.ai",
                port=9444,
                profile=Path("worker-profile"),
                isolated=True,
            )

        session.close()

        pw.stop.assert_called_once()
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=2)

    def test_non_isolated_session_close_does_not_terminate_browser_process(self) -> None:
        pw = mock.Mock()
        session = browser.Session(pw=pw, browser=mock.Mock(), page=mock.Mock())

        session.close()

        pw.stop.assert_called_once()

    def test_isolated_launch_failure_terminates_process(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch.object(browser, "_find_free_isolated_cdp_port", return_value=9444),
            mock.patch.object(browser, "_launch_browser", return_value=process),
            mock.patch.object(browser, "_wait_port", side_effect=TimeoutError("no port")),
        ):
            with self.assertRaisesRegex(TimeoutError, "no port"):
                browser._ensure_cdp_endpoint(
                    preferred=9444,
                    profile=Path("worker-profile"),
                    start_url="https://chat.qwen.ai/",
                    url_contains="chat.qwen.ai",
                    open_if_missing=True,
                    isolated=True,
                )

        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=2)

    def test_isolated_launch_retries_next_free_port_after_timeout(self) -> None:
        failed_process = mock.Mock()
        failed_process.poll.return_value = None
        ok_process = mock.Mock()
        with (
            mock.patch.object(
                browser,
                "_find_free_isolated_cdp_port",
                side_effect=[9444, 9445],
            ) as find_port,
            mock.patch.object(
                browser,
                "_launch_browser",
                side_effect=[failed_process, ok_process],
            ) as launch,
            mock.patch.object(
                browser,
                "_wait_port",
                side_effect=[TimeoutError("no port"), None],
            ) as wait_port,
        ):
            endpoint = browser._ensure_cdp_endpoint(
                preferred=9444,
                profile=Path("worker-profile"),
                start_url="https://chat.qwen.ai/",
                url_contains="chat.qwen.ai",
                open_if_missing=True,
                isolated=True,
            )

        self.assertEqual(endpoint, browser.CdpEndpoint(9445, ok_process))
        self.assertEqual(find_port.call_args_list[0].kwargs["exclude"], set())
        self.assertEqual(find_port.call_args_list[1].kwargs["exclude"], {9444})
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(
            launch.call_args_list[0].args,
            (9444, Path("worker-profile") / "isolated-9444", "https://chat.qwen.ai/"),
        )
        self.assertEqual(
            launch.call_args_list[1].args,
            (9445, Path("worker-profile") / "isolated-9445", "https://chat.qwen.ai/"),
        )
        self.assertEqual(wait_port.call_args_list[0].args, (9444,))
        self.assertEqual(wait_port.call_args_list[1].args, (9445,))
        failed_process.terminate.assert_called_once()

    def test_isolated_launch_cancellation_terminates_process_without_retry(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch.object(browser, "_find_free_isolated_cdp_port", side_effect=[9444, 9445]) as find_port,
            mock.patch.object(browser, "_launch_browser", return_value=process) as launch,
            mock.patch.object(
                browser,
                "_wait_port",
                side_effect=browser.cancellation.TaskCancelled("stop"),
            ),
        ):
            with self.assertRaises(browser.cancellation.TaskCancelled):
                browser._ensure_cdp_endpoint(
                    preferred=9444,
                    profile=Path("worker-profile"),
                    start_url="https://chat.qwen.ai/",
                    url_contains="chat.qwen.ai",
                    open_if_missing=True,
                    isolated=True,
                )

        find_port.assert_called_once()
        launch.assert_called_once()
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=2)

    def test_non_isolated_launch_failure_terminates_process(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        with (
            mock.patch.object(browser, "_find_cdp_port_with_target", return_value=None),
            mock.patch.object(browser, "_find_existing_cdp_port", return_value=None),
            mock.patch.object(browser, "_find_free_cdp_port", return_value=9444),
            mock.patch.object(browser, "_launch_browser", return_value=process),
            mock.patch.object(browser, "_wait_port", side_effect=TimeoutError("no port")),
        ):
            with self.assertRaisesRegex(TimeoutError, "no port"):
                browser._ensure_cdp_endpoint(
                    preferred=9444,
                    profile=Path("profile"),
                    start_url="https://chat.qwen.ai/",
                    url_contains="chat.qwen.ai",
                    open_if_missing=True,
                )

        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=2)

    def test_ensure_cdp_browser_endpoint_reuses_existing_browser(self) -> None:
        with (
            mock.patch.object(browser, "_find_remembered_cdp_port", return_value=9333),
            mock.patch.object(browser, "_launch_browser") as launch,
        ):
            endpoint = browser._ensure_cdp_browser_endpoint(
                preferred=9222,
                profile=Path("profile"),
                start_url=browser.DEEPSEEK_URL,
            )

        self.assertEqual(endpoint.port, 9333)
        self.assertIsNone(endpoint.process)
        launch.assert_not_called()

    def test_ensure_cdp_browser_endpoint_does_not_reuse_unremembered_cdp(self) -> None:
        process = mock.Mock()
        with (
            mock.patch.object(browser, "_find_existing_cdp_port", return_value=9333) as find_existing,
            mock.patch.object(browser, "_find_remembered_cdp_port", return_value=None),
            mock.patch.object(browser, "_find_free_cdp_port", return_value=9444),
            mock.patch.object(browser, "_launch_browser", return_value=process) as launch,
            mock.patch.object(browser, "_wait_port"),
            mock.patch.object(browser, "_remember_cdp_port", return_value=9444),
        ):
            endpoint = browser._ensure_cdp_browser_endpoint(
                preferred=9222,
                profile=Path("profile"),
                start_url=browser.DEEPSEEK_URL,
            )

        self.assertEqual(endpoint, browser.CdpEndpoint(9444, process))
        find_existing.assert_not_called()
        launch.assert_called_once_with(9444, Path("profile"), browser.DEEPSEEK_URL)

    def test_ensure_cdp_browser_endpoint_launches_browser_when_missing(self) -> None:
        process = mock.Mock()
        with (
            mock.patch.object(browser, "_find_remembered_cdp_port", return_value=None),
            mock.patch.object(browser, "_find_free_cdp_port", return_value=9444),
            mock.patch.object(browser, "_launch_browser", return_value=process) as launch,
            mock.patch.object(browser, "_wait_port") as wait_port,
            mock.patch.object(browser, "_remember_cdp_port", return_value=9444) as remember,
        ):
            endpoint = browser._ensure_cdp_browser_endpoint(
                preferred=9222,
                profile=Path("profile"),
                start_url=browser.DEEPSEEK_URL,
            )

        self.assertEqual(endpoint, browser.CdpEndpoint(9444, process))
        launch.assert_called_once_with(9444, Path("profile"), browser.DEEPSEEK_URL)
        wait_port.assert_called_once_with(9444, timeout=20.0)
        remember.assert_called_once_with(9444)

    def test_warm_provider_tabs_returns_existing_tabs_without_opening_pages(self) -> None:
        statuses = {"deepseek": True, "qwen": False, "mimo": False, "stepfun": False, "glm": False}
        with (
            mock.patch.object(browser, "detect_open_provider_tabs", return_value=statuses),
            mock.patch.object(browser, "_ensure_cdp_browser_endpoint") as ensure,
            mock.patch.object(browser, "_start_playwright_with_retry") as start_pw,
        ):
            result = browser.warm_provider_tabs()

        self.assertEqual(result, statuses)
        ensure.assert_not_called()
        start_pw.assert_not_called()

    def test_warm_provider_tabs_opens_all_provider_pages_on_empty_existing_cdp(self) -> None:
        empty = {"deepseek": False, "qwen": False, "mimo": False, "stepfun": False, "glm": False}
        final = {"deepseek": True, "qwen": True, "mimo": True, "stepfun": True, "glm": True}
        pages = [mock.Mock(url=url) for url in browser.PROVIDER_START_URLS.values()]
        ctx = mock.Mock(pages=[])
        ctx.new_page.side_effect = pages
        browser_obj = mock.Mock(contexts=[ctx])
        pw = mock.Mock()
        pw.chromium.connect_over_cdp.return_value = browser_obj

        with (
            mock.patch.object(browser, "detect_open_provider_tabs", side_effect=[empty, final]),
            mock.patch.object(
                browser,
                "_ensure_cdp_browser_endpoint",
                return_value=browser.CdpEndpoint(9333),
            ) as ensure,
            mock.patch.object(browser, "_start_playwright_with_retry", return_value=pw),
        ):
            result = browser.warm_provider_tabs()

        self.assertEqual(result, final)
        ensure.assert_called_once_with(
            preferred=browser.DEFAULT_PORT,
            profile=browser.DEFAULT_PROFILE,
            start_url=browser.DEEPSEEK_URL,
            wait_timeout=browser.WARMUP_PORT_TIMEOUT,
        )
        pw.chromium.connect_over_cdp.assert_called_once_with(
            "http://127.0.0.1:9333",
            timeout=browser.WARMUP_CDP_CONNECT_TIMEOUT_MS,
        )
        self.assertEqual(ctx.new_page.call_count, 5)
        self.assertEqual(
            [page.goto.call_args.args[0] for page in pages],
            list(browser.PROVIDER_START_URLS.values()),
        )
        self.assertTrue(
            all(
                page.goto.call_args.kwargs["timeout"]
                == browser.WARMUP_NAVIGATION_TIMEOUT_MS
                for page in pages
            )
        )
        pw.stop.assert_called_once_with()

    def test_warm_provider_tabs_opens_first_provider_when_launch_page_is_not_visible(self) -> None:
        empty = {"deepseek": False, "qwen": False, "mimo": False, "stepfun": False, "glm": False}
        final = {"deepseek": True, "qwen": True, "mimo": True, "stepfun": True, "glm": True}
        pages = [mock.Mock(url=url) for url in browser.PROVIDER_START_URLS.values()]
        ctx = mock.Mock(pages=[])
        ctx.new_page.side_effect = pages
        browser_obj = mock.Mock(contexts=[ctx])
        pw = mock.Mock()
        pw.chromium.connect_over_cdp.return_value = browser_obj

        with (
            mock.patch.object(browser, "detect_open_provider_tabs", side_effect=[empty, final]),
            mock.patch.object(
                browser,
                "_ensure_cdp_browser_endpoint",
                return_value=browser.CdpEndpoint(9444, mock.Mock()),
            ),
            mock.patch.object(browser, "_start_playwright_with_retry", return_value=pw),
        ):
            result = browser.warm_provider_tabs()

        self.assertEqual(result, final)
        self.assertEqual(ctx.new_page.call_count, 5)
        self.assertEqual(
            [page.goto.call_args.args[0] for page in pages],
            list(browser.PROVIDER_START_URLS.values()),
        )

    def test_warm_provider_tabs_skips_launch_start_page_when_visible(self) -> None:
        empty = {"deepseek": False, "qwen": False, "mimo": False, "stepfun": False, "glm": False}
        final = {"deepseek": True, "qwen": True, "mimo": True, "stepfun": True, "glm": True}
        launch_page = mock.Mock(url=browser.DEEPSEEK_URL)
        pages = [mock.Mock(url=url) for url in list(browser.PROVIDER_START_URLS.values())[1:]]
        ctx = mock.Mock(pages=[launch_page])
        ctx.new_page.side_effect = pages
        browser_obj = mock.Mock(contexts=[ctx])
        pw = mock.Mock()
        pw.chromium.connect_over_cdp.return_value = browser_obj

        with (
            mock.patch.object(browser, "detect_open_provider_tabs", side_effect=[empty, final]),
            mock.patch.object(
                browser,
                "_ensure_cdp_browser_endpoint",
                return_value=browser.CdpEndpoint(9444, mock.Mock()),
            ),
            mock.patch.object(browser, "_start_playwright_with_retry", return_value=pw),
        ):
            result = browser.warm_provider_tabs()

        self.assertEqual(result, final)
        self.assertEqual(ctx.new_page.call_count, 4)
        self.assertEqual(
            [page.goto.call_args.args[0] for page in pages],
            list(browser.PROVIDER_START_URLS.values())[1:],
        )

    def test_warm_provider_tabs_continues_when_one_provider_page_fails(self) -> None:
        empty = {"deepseek": False, "qwen": False, "mimo": False, "stepfun": False, "glm": False}
        final = {"deepseek": True, "qwen": False, "mimo": True, "stepfun": True, "glm": True}
        pages = [mock.Mock(url=url) for url in browser.PROVIDER_START_URLS.values()]
        pages[1].url = "about:blank"
        pages[1].goto.side_effect = RuntimeError("navigation failed")
        ctx = mock.Mock(pages=[])
        ctx.new_page.side_effect = pages
        browser_obj = mock.Mock(contexts=[ctx])
        pw = mock.Mock()
        pw.chromium.connect_over_cdp.return_value = browser_obj

        with (
            mock.patch.object(browser, "detect_open_provider_tabs", side_effect=[empty, final]),
            mock.patch.object(
                browser,
                "_ensure_cdp_browser_endpoint",
                return_value=browser.CdpEndpoint(9333),
            ),
            mock.patch.object(browser, "_start_playwright_with_retry", return_value=pw),
        ):
            result = browser.warm_provider_tabs()

        self.assertEqual(result, final)
        self.assertEqual(ctx.new_page.call_count, 5)
        self.assertEqual(pages[0].goto.call_count, 1)
        pages[1].close.assert_called_once_with()
        self.assertEqual(pages[2].goto.call_count, 1)
        self.assertEqual(pages[3].goto.call_count, 1)

    def test_warm_provider_tabs_keeps_slow_page_when_provider_url_is_reached(self) -> None:
        empty = {"deepseek": False, "qwen": False, "mimo": False, "stepfun": False, "glm": False}
        final = {"deepseek": True, "qwen": True, "mimo": True, "stepfun": True, "glm": True}
        pages = [mock.Mock(url=url) for url in browser.PROVIDER_START_URLS.values()]
        pages[1].goto.side_effect = TimeoutError("domcontentloaded timed out")
        ctx = mock.Mock(pages=[])
        ctx.new_page.side_effect = pages
        browser_obj = mock.Mock(contexts=[ctx])
        pw = mock.Mock()
        pw.chromium.connect_over_cdp.return_value = browser_obj

        with (
            mock.patch.object(browser, "detect_open_provider_tabs", side_effect=[empty, final]),
            mock.patch.object(
                browser,
                "_ensure_cdp_browser_endpoint",
                return_value=browser.CdpEndpoint(9333),
            ),
            mock.patch.object(browser, "_start_playwright_with_retry", return_value=pw),
        ):
            result = browser.warm_provider_tabs()

        self.assertEqual(result, final)
        self.assertEqual(ctx.new_page.call_count, 5)
        pages[1].close.assert_not_called()
        self.assertEqual(pages[2].goto.call_count, 1)
        self.assertEqual(pages[3].goto.call_count, 1)


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