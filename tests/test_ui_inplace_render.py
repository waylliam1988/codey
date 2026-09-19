from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from codey.app import server as codey_server

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


def _running_in_ci() -> bool:
    return any(os.environ.get(name, "").lower() not in {"", "0", "false"} for name in ("CI", "GITHUB_ACTIONS"))


def _skip_or_fail_browser_unavailable(message: str, exc: Exception | None = None) -> None:
    if _running_in_ci():
        raise AssertionError(message) from exc
    raise unittest.SkipTest(message)


def _join_live_handler_threads(httpd: object, timeout: float = 0.5) -> list[object]:
    threads = list(getattr(httpd, "_threads", ()))
    for thread in threads:
        if thread.is_alive():
            thread.join(timeout=timeout)
    return [thread for thread in threads if thread.is_alive()]


class UiInPlaceRenderHarnessTests(unittest.TestCase):
    def test_browser_unavailable_skips_outside_ci(self) -> None:
        with mock.patch.dict(os.environ, {"CI": "", "GITHUB_ACTIONS": ""}):
            with self.assertRaises(unittest.SkipTest):
                _skip_or_fail_browser_unavailable("missing browser", RuntimeError("boom"))

    def test_browser_unavailable_fails_in_ci(self) -> None:
        with mock.patch.dict(os.environ, {"CI": "true"}):
            with self.assertRaises(AssertionError):
                _skip_or_fail_browser_unavailable("missing browser", RuntimeError("boom"))

    def test_join_live_handler_threads_returns_threads_that_remain_alive(self) -> None:
        class FakeThread:
            def __init__(self, alive: bool) -> None:
                self.alive = alive
                self.join_timeout: float | None = None

            def is_alive(self) -> bool:
                return self.alive

            def join(self, timeout: float | None = None) -> None:
                self.join_timeout = timeout

        finished = FakeThread(False)
        stuck = FakeThread(True)
        lingering = _join_live_handler_threads(mock.Mock(_threads=[finished, stuck]), timeout=0.25)

        self.assertEqual(lingering, [stuck])
        self.assertIsNone(finished.join_timeout)
        self.assertEqual(stuck.join_timeout, 0.25)


class UiInPlaceRenderBrowserTests(unittest.TestCase):
    """Browser-level validation of in-place tool replacement, text selection

    preservation, and scroll policy.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if sync_playwright is None:
            _skip_or_fail_browser_unavailable("playwright is required for browser DOM tests")
        try:
            with sync_playwright() as pw:
                probe = pw.chromium.launch(headless=True)
                probe.close()
        except Exception as exc:
            _skip_or_fail_browser_unavailable(f"playwright chromium browser is not available: {exc}", exc)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = codey_server.AppContext(Path(cls.tmp.name) / "state")
        cls.state_patch = mock.patch.object(codey_server, "STATE", cls.state)
        cls.state_patch.start()
        cls.httpd = codey_server.CodeyHTTPServer(("127.0.0.1", 0), codey_server.Handler)
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        host, port = cls.httpd.server_address
        cls.base_url = f"http://{host}:{port}/"

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            if hasattr(cls, "httpd"):
                cls.httpd.shutdown()
                cls.httpd.server_close()
            if hasattr(cls, "server_thread"):
                cls.server_thread.join(timeout=2.0)
            lingering_handlers = []
            if hasattr(cls, "httpd") and hasattr(cls.httpd, "_threads"):
                lingering_handlers = _join_live_handler_threads(cls.httpd)
            if hasattr(cls, "state"):
                cls.state.close()
            if hasattr(cls, "tmp"):
                cls.tmp.cleanup()
            if lingering_handlers:
                raise AssertionError(f"{len(lingering_handlers)} request handler thread(s) did not stop")
        finally:
            if hasattr(cls, "state_patch"):
                cls.state_patch.stop()

    def test_in_place_tool_render_preserves_assistant_dom_and_selection_and_respects_scroll(self) -> None:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1024, "height": 768})
                page.goto(self.base_url)
                page.wait_for_function("typeof window.renderChat === 'function'")

                result = page.evaluate("""() => {
                    const sid = 'test-session-inplace';
                    const initialMessages = [
                        { type: 'user', text: 'Please inspect and modify the target module.' },
                        { type: 'asst', text: 'I am reading the codebase and executing edits now.' },
                        { type: 'tool_pending', kind: 'edit', toolKey: 'tool-k1', path: 'module.py', activity: 'Applying changes...' }
                    ];

                    window.CodeyUiState.apply({
                        active_id: sid,
                        sessions: [{
                            id: sid,
                            title: 'In-place Test',
                            projectId: null,
                            provider: 'deepseek',
                            messages: initialMessages,
                            terminalRuns: []
                        }],
                        projects: []
                    });

                    // Initial chat rendering
                    window.renderChat(true);

                    const chatEl = document.getElementById('chat');
                    const chatArea = document.getElementById('chat-area');

                    const asstNode = chatEl.querySelector('.msg.asst');
                    if (!asstNode) throw new Error('Expected assistant message node');
                    asstNode.__identity_marker = 'original-asst-instance';

                    const pendingToolNode = chatEl.querySelector('.msg.tool[data-tool-key="tool-k1"]');
                    if (!pendingToolNode) throw new Error('Expected pending tool node');

                    // 1. Select text within the assistant message
                    const selection = window.getSelection();
                    selection.removeAllRanges();
                    const range = document.createRange();
                    range.selectNodeContents(asstNode.querySelector('.body') || asstNode);
                    selection.addRange(range);
                    const selectedTextBefore = selection.toString();

                    // Make the chat area scrollable to test scroll policies
                    const spacer = document.createElement('div');
                    spacer.id = 'test-scroll-spacer';
                    spacer.style.height = '1200px';
                    chatEl.appendChild(spacer);

                    const maxScroll = chatArea.scrollHeight - chatArea.clientHeight;

                    // 2. Simulate user reading history away from the bottom (e.g. top)
                    chatArea.scrollTop = 0;
                    const distanceToBottomBefore = maxScroll - chatArea.scrollTop;

                    // 3. Trigger final tool message via replaceSessionMessage
                    const finalToolMessage = {
                        type: 'tool',
                        kind: 'edit',
                        toolKey: 'tool-k1',
                        path: 'module.py',
                        result: 'Successfully updated 1 line'
                    };

                    const replaced = window.replaceSessionMessage(
                        sid,
                        m => m.type === 'tool_pending' && m.toolKey === 'tool-k1',
                        finalToolMessage
                    );

                    // 4. Assertions on DOM identity & selection preservation
                    const currentAsstNode = chatEl.querySelector('.msg.asst');
                    const asstNodeIdentical = (currentAsstNode === asstNode);
                    const asstMarkerPreserved = (currentAsstNode.__identity_marker === 'original-asst-instance');
                    const selectedTextAfter = selection.toString();
                    const selectionPreserved = (selectedTextAfter === selectedTextBefore);

                    // 5. Assertions on tool node in-place replacement
                    const currentToolNode = chatEl.querySelector('.msg.tool[data-tool-key="tool-k1"]');
                    const toolNodeReplaced = (currentToolNode !== pendingToolNode);
                    const toolHasFinalResult = currentToolNode ? currentToolNode.textContent.includes('Successfully updated 1 line') : false;
                    const toolPendingRemoved = currentToolNode ? currentToolNode.querySelector('.tool-line.pending') === null : false;

                    // 6. Assertions on scroll policy:
                    // When away from bottom (scrollTop = 0, distance > 120px), replace does NOT force jump to bottom
                    const scrollAwayPreserved = (chatArea.scrollTop === 0);

                    // Near bottom follow: when within 120px of bottom, scrollChat() follows to bottom
                    chatArea.scrollTop = maxScroll - 50; // 50px < 120px
                    window.scrollChat(false);
                    const nearBottomFollowed = (chatArea.scrollTop >= maxScroll - 1);

                    // Forced follow: scrollChat(true) always forces to bottom
                    chatArea.scrollTop = 0;
                    window.scrollChat(true);
                    const forcedFollowSucceeded = (chatArea.scrollTop >= maxScroll - 1);

                    return {
                        replaced,
                        asstNodeIdentical,
                        asstMarkerPreserved,
                        selectionPreserved,
                        selectedTextBefore,
                        selectedTextAfter,
                        toolNodeReplaced,
                        toolHasFinalResult,
                        toolPendingRemoved,
                        scrollAwayPreserved,
                        nearBottomFollowed,
                        forcedFollowSucceeded
                    };
                }""")
            finally:
                browser.close()

        self.assertTrue(result["replaced"])
        self.assertTrue(result["asstNodeIdentical"], "Assistant DOM node must not be rebuilt")
        self.assertTrue(result["asstMarkerPreserved"], "Custom marker on assistant node must be preserved")
        self.assertTrue(result["selectionPreserved"], "Active text selection must be preserved across tool replacement")
        self.assertIn("reading the codebase", result["selectedTextBefore"])
        self.assertTrue(result["toolNodeReplaced"], "Pending tool DOM element must be replaced by final tool node")
        self.assertTrue(result["toolHasFinalResult"], "Final tool node must display final result text")
        self.assertTrue(result["toolPendingRemoved"], "Pending styling must be removed on final tool node")
        self.assertTrue(result["scrollAwayPreserved"], "Scroll position must not be forced to bottom when reading history")
        self.assertTrue(result["nearBottomFollowed"], "Scroll must follow to bottom when user is near bottom (<120px)")
        self.assertTrue(result["forcedFollowSucceeded"], "Forced scroll must always reach the bottom")


if __name__ == "__main__":
    unittest.main()
