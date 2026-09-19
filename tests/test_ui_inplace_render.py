from __future__ import annotations

import threading
import unittest

from codey.app import server as codey_server

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


class UiInPlaceRenderBrowserTests(unittest.TestCase):
    """Browser-level validation of in-place tool replacement, text selection

    preservation, and scroll policy.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if sync_playwright is None:
            raise unittest.SkipTest("playwright is required for browser DOM tests")
        cls.httpd = codey_server.CodeyHTTPServer(("127.0.0.1", 0), codey_server.Handler)
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        host, port = cls.httpd.server_address
        cls.base_url = f"http://{host}:{port}/"

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "httpd"):
            cls.httpd.shutdown()
            cls.httpd.server_close()

    def test_in_place_tool_render_preserves_assistant_dom_and_selection_and_respects_scroll(self) -> None:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
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
