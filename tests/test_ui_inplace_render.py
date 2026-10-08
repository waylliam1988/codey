from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from codey.app import server as codey_server


def _attach_page_diagnostics(page) -> dict[str, object]:
    diag: dict[str, object] = {
        "pageerrors": [],
        "console_errors": [],
        "request_failed": [],
        "bad_responses": [],
        "goto_status": None,
    }

    def _on_pageerror(exc: object) -> None:
        errors = diag["pageerrors"]
        assert isinstance(errors, list)
        errors.append(str(exc)[:500])

    def _on_console(message: object) -> None:
        msg_type = getattr(message, "type", "")
        if msg_type == "error":
            errors = diag["console_errors"]
            assert isinstance(errors, list)
            errors.append(str(getattr(message, "text", message))[:500])

    def _on_request_failed(request: object) -> None:
        failed = diag["request_failed"]
        assert isinstance(failed, list)
        failed.append(
            f"{getattr(request, 'method', '?')} {getattr(request, 'url', '?')} :: "
            f"{getattr(request, 'failure', '?')}"[:300]
        )

    def _on_response(response: object) -> None:
        try:
            status = int(getattr(response, "status", 0))
        except (TypeError, ValueError):
            return
        url = str(getattr(response, "url", ""))
        if status >= 400 and ("127.0.0.1" in url or "localhost" in url):
            bad = diag["bad_responses"]
            assert isinstance(bad, list)
            bad.append(f"{status} {url}"[:300])

    with_ = getattr(page, "on", None)
    if callable(with_):
        with_("pageerror", _on_pageerror)
        with_("console", _on_console)
        with_("requestfailed", _on_request_failed)
        with_("response", _on_response)
    return diag


def _format_diagnostics(diag: dict[str, object]) -> str:
    try:
        return json.dumps(diag, ensure_ascii=False)[:2000]
    except Exception:
        return str(diag)[:2000]


def _goto_restored_ui(page, url):
    # Observe the actual boot restore promise before index.html binds its alias.
    # DOM tests must not race a late renderChat() from server-state restoration.
    page.add_init_script("""
        Object.defineProperty(window, 'CodeyUiState', {configurable:true, set(state) {
            Object.defineProperty(window, 'CodeyUiState', {
                value:state, writable:true, configurable:true, enumerable:true});
            const restore = state.restoreFromServer;
            state.restoreFromServer = async function(...args) {
                const result = await restore.apply(this, args);
                window.__inPlaceRestoreDone = true;
                return result;
            };
        }});
    """)
    response = page.goto(url)
    page.wait_for_function("window.__inPlaceRestoreDone === true", polling=100)
    return response


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
        with mock.patch.dict(os.environ, {"CI": "", "GITHUB_ACTIONS": ""}), self.assertRaises(unittest.SkipTest):
            _skip_or_fail_browser_unavailable("missing browser", RuntimeError("boom"))

    def test_browser_unavailable_fails_in_ci(self) -> None:
        with mock.patch.dict(os.environ, {"CI": "true"}), self.assertRaises(AssertionError):
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

    def test_process_history_and_explicit_disclosures_survive_reconciliation(self) -> None:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                _goto_restored_ui(page, self.httpd.launch_url(self.base_url))
                result = page.evaluate("""() => {
                    const sid = 'history-process';
                    const messages = [
                        {type:'user', text:'First request'},
                        {type:'turn', n:1, runId:'old', reasoning:'* Inspect this.\\n* Keep the answer separate.'},
                        {type:'tool', kind:'read', path:'one.py', runId:'old', toolKey:'old:1'},
                        {type:'tool', kind:'read', path:'extra.py', runId:'old', toolKey:'old:2'},
                        {type:'asst', text:'Finished first request.', runId:'old'},
                        {type:'user', text:'Second request'},
                        {type:'run_state', state:'running', runId:'new'},
                        {type:'turn', n:1, runId:'new'},
                        {type:'tool_pending', kind:'read', path:'two.py', runId:'new', toolKey:'new:1'},
                    ];
                    const snapshot = rows => ({active_id:sid, projects:[], sessions:[{
                        id:sid, title:'History', provider:'local', messages:rows, terminalRuns:[]
                    }]});
                    window.CodeyUiState.apply(snapshot(messages)); window.renderChat();
                    const chat = document.getElementById('chat');
                    const first = chat.querySelector('.process-group');
                    const historical = !first.open && first.querySelector('summary').textContent.startsWith('Worked');
                    first.querySelector('summary').click();
                    first.querySelector('.thinking > summary').click();
                    first.querySelector('.tool-group-summary').click();
                    const originalAnswer = chat.querySelector('.msg.asst .body');
                    const originalRange = document.createRange(); originalRange.selectNodeContents(originalAnswer);
                    getSelection().removeAllRanges(); getSelection().addRange(originalRange);
                    const originalSelected = getSelection().toString();
                    // Equal backend snapshots preserve message DOM and pointer selection.
                    window.CodeyUiState.apply(snapshot(JSON.parse(JSON.stringify(messages)))); window.renderChat();
                    const snapshotStable = originalAnswer === chat.querySelector('.msg.asst .body')
                        && getSelection().toString() === originalSelected;
                    // A genuinely changed snapshot rebuilds while keeping disclosure choices.
                    window.CodeyUiState.apply(snapshot([...messages, {type:'info', text:'Reconciled'}])); window.renderChat();
                    const rebuilt = chat.querySelector('.process-group');
                    const kept = rebuilt.open && rebuilt.querySelector('.thinking').open
                        && !rebuilt.querySelector('.tool-group').classList.contains('collapsed');
                    const answer = chat.querySelector('.msg.asst .body');
                    const range = document.createRange(); range.selectNodeContents(answer);
                    getSelection().removeAllRanges(); getSelection().addRange(range);
                    const selected = getSelection().toString();
                    window.replaceSessionMessage(sid, m => m.toolKey === 'new:1', {type:'tool', kind:'read', path:'two.py',
                        result:'3 lines', runId:'new', toolKey:'new:1'});
                    return {historical, kept, snapshotStable, groups:chat.querySelectorAll('.process-group').length,
                        selected, after:getSelection().toString(), sameAnswer:answer === chat.querySelector('.msg.asst .body'),
                        reasoningList:!!rebuilt.querySelector('.thinking .md-list'),
                        blankThinking:chat.querySelectorAll('.thinking').length};
                }""")
                self.assertTrue(result["historical"])
                self.assertTrue(result["kept"])
                self.assertTrue(result["snapshotStable"])
                self.assertEqual(result["groups"], 2)
                self.assertEqual(result["blankThinking"], 1)
                self.assertTrue(result["sameAnswer"])
                self.assertTrue(result["reasoningList"])
                self.assertEqual(result["selected"], result["after"])
            finally:
                browser.close()

    def test_process_group_keeps_final_answer_approval_and_errors_visible(self) -> None:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                _goto_restored_ui(page, self.httpd.launch_url(self.base_url))
                result = page.evaluate("""() => {
                    const chat = document.getElementById('chat'); chat.replaceChildren();
                    const rows = [
                        {type:'user', text:'Fix the issue'},
                        {type:'turn', n:1, runId:'r', reasoning:'Inspect the source.'},
                        {type:'tool', kind:'read', path:'a.py', result:'12 lines', runId:'r', toolKey:'r:1:0'},
                        {type:'turn', n:2, runId:'r'},
                        {type:'tool', kind:'read', path:'b.py', result:'20 lines', runId:'r', toolKey:'r:2:0'},
                        {type:'tool', kind:'run', path:'', result:'exit 1', error:true, runId:'r', toolKey:'r:2:1'},
                        {type:'shell_request', id:'approval1', command:'npm test', cwd:'.', runId:'r'},
                        {type:'asst', text:'The final answer stays readable.', runId:'r'},
                        {type:'run_state', state:'failed', runId:'r'},
                    ];
                    rows.forEach(m => window.appendMessageNode(chat, m));
                    const group = chat.querySelector('.process-group');
                    if (!group) throw new Error('No process group');
                    const visible = node => !!node && !!node.getClientRects().length;
                    const answer = chat.querySelector('.msg.asst .body');
                    const approval = chat.querySelector('[data-approval-id]');
                    const error = chat.querySelector('.tool-line.error');
                    const thinking = group.querySelector('.thinking');
                    const closed = !group.open;
                    group.open = true;
                    return {closed, answer:visible(answer), approval:visible(approval), error:visible(error),
                        reasoning:thinking?.textContent, thinkingClosed:thinking && !thinking.open,
                        toolGroups:group.querySelectorAll('.tool-group').length,
                        summary:group.querySelector('summary').textContent,
                        turnDividers:chat.querySelectorAll(':scope > .turn-divider').length};
                }""")
                self.assertTrue(result["closed"])
                self.assertTrue(result["answer"])
                self.assertTrue(result["approval"])
                self.assertTrue(result["error"])
                self.assertTrue(result["thinkingClosed"])
                self.assertIn("Inspect the source.", result["reasoning"])
                self.assertEqual(result["toolGroups"], 1)
                self.assertIn("Failed", result["summary"])
                self.assertEqual(result["turnDividers"], 0)
            finally:
                browser.close()

    def test_markdown_tables_links_and_copy_feedback_are_safe_and_readable(self) -> None:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                _goto_restored_ui(page, self.httpd.launch_url(self.base_url))
                result = page.evaluate("""async () => {
                    const chat = document.getElementById('chat'); chat.replaceChildren();
                    const text = '[Docs](https://example.com/docs) [bad](javascript:alert)\\n\\n'
                        + '| Name | Result |\\n| --- | --- |\\n| 中文 | **OK** |\\n\\n'
                        + '<img src=x onerror=alert(1)>\\n\\n```js\\nconst x = 1;\\n```'
                        + '\\n\\n* Parent\\n    * First\\n    * Second';
                    window.appendMessageNode(chat, {type:'asst', text});
                    let copied = '';
                    Object.defineProperty(navigator, 'clipboard', {configurable:true,
                        value:{writeText:async value => {copied=value;}}});
                    const button = chat.querySelector('.msg-copy'); button.click();
                    await new Promise(resolve => setTimeout(resolve, 20));
                    return {tables:chat.querySelectorAll('table').length, cells:chat.querySelectorAll('td').length,
                        links:chat.querySelectorAll('a').length, href:chat.querySelector('a')?.getAttribute('href'),
                        dangerous:chat.querySelectorAll('img, script, a[href^="javascript:"]').length,
                        copied, label:button.getAttribute('aria-label'), check:!!button.querySelector('.copy-check'),
                        codeCopy:!!chat.querySelector('.code-copy'),
                        siblings:chat.querySelectorAll('.md-list > li > .md-list > li').length,
                        extraNesting:chat.querySelectorAll('.md-list .md-list .md-list').length};
                }""")
                self.assertEqual(result["tables"], 1)
                self.assertEqual(result["cells"], 2)
                self.assertEqual(result["links"], 1)
                self.assertEqual(result["href"], "https://example.com/docs")
                self.assertEqual(result["dangerous"], 0)
                self.assertIn("| Name |", result["copied"])
                self.assertEqual(result["label"], "Copied")
                self.assertTrue(result["check"])
                self.assertTrue(result["codeCopy"])
                self.assertEqual(result["siblings"], 2)
                self.assertEqual(result["extraNesting"], 0)
            finally:
                browser.close()

    def test_in_place_tool_render_preserves_assistant_dom_and_selection_and_respects_scroll(self) -> None:
        diag: dict[str, object] = {}
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1024, "height": 768})
                diag = _attach_page_diagnostics(page)
                try:
                    response = _goto_restored_ui(page, self.httpd.launch_url(self.base_url))
                    diag["goto_status"] = response.status if response else None
                except Exception as exc:
                    raise AssertionError(
                        f"homepage/scripts failed :: diag={_format_diagnostics(diag)}"
                    ) from exc

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
                    window.renderChat();

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
                    chatArea.dispatchEvent(new WheelEvent('wheel', {deltaY:-80}));
                    chatArea.scrollTop = 0;
                    window.CodeyConversationUI.captureView();
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

                    // Following resumes only through the explicit latest action.
                    document.getElementById('back-to-latest').click();
                    const explicitFollowSucceeded = (chatArea.scrollTop >= maxScroll - 1);

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
                        explicitFollowSucceeded
                    };
                }""")
            finally:
                browser.close()

        try:
            self.assertTrue(result["replaced"])
            self.assertTrue(result["asstNodeIdentical"], "Assistant DOM node must not be rebuilt")
            self.assertTrue(result["asstMarkerPreserved"], "Custom marker on assistant node must be preserved")
            self.assertTrue(result["selectionPreserved"], "Active text selection must be preserved across tool replacement")
            self.assertIn("reading the codebase", result["selectedTextBefore"])
            self.assertTrue(result["toolNodeReplaced"], "Pending tool DOM element must be replaced by final tool node")
            self.assertTrue(result["toolHasFinalResult"], "Final tool node must display final result text")
            self.assertTrue(result["toolPendingRemoved"], "Pending styling must be removed on final tool node")
            self.assertTrue(result["scrollAwayPreserved"], "Scroll position must not be forced to bottom when reading history")
            self.assertTrue(result["explicitFollowSucceeded"], "Explicit latest action must reach the bottom")
        except AssertionError as exc:
            raise AssertionError(f"{exc} :: diag={_format_diagnostics(diag)}") from exc

    def test_shell_result_titles_never_show_failed_runs_as_executed(self) -> None:
        diag: dict[str, object] = {}
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1024, "height": 768})
                diag = _attach_page_diagnostics(page)
                try:
                    response = _goto_restored_ui(page, self.httpd.launch_url(self.base_url))
                    diag["goto_status"] = response.status if response else None
                except Exception as exc:
                    raise AssertionError(
                        f"homepage/scripts failed :: diag={_format_diagnostics(diag)}"
                    ) from exc

                result = page.evaluate("""() => {
                    const chat = document.createElement('div');
                    const cases = [
                        { key: 'exit|true', shellStatus: 'exit', approved: true },
                        // Real denial events carry approved=false with no status.
                        { key: 'empty|denied', shellStatus: '', approved: false },
                        { key: 'missing|denied', approved: false },
                        { key: 'exit|denied', shellStatus: 'exit', approved: false },
                        { key: 'stopped|false', shellStatus: 'stopped', approved: false },
                        { key: 'timeout|true', shellStatus: 'timeout', approved: true },
                        { key: 'spawn_error|true', shellStatus: 'spawn_error', approved: true },
                        { key: 'wait_error|true', shellStatus: 'wait_error', approved: true },
                        { key: 'output_read_error|true', shellStatus: 'output_read_error', approved: true },
                        { key: 'drain_timeout|true', shellStatus: 'drain_timeout', approved: true },
                        { key: 'future_unknown_status|true', shellStatus: 'future_unknown_status', approved: true },
                        { key: 'empty|approved', shellStatus: '', approved: true },
                    ];
                    const titles = {};
                    for (const c of cases) {
                        chat.innerHTML = '';
                        const event = {
                            type: 'shell_result',
                            approved: c.approved,
                            command: 'pytest -q',
                            exitCode: c.shellStatus === 'exit' ? 0 : null,
                            output: 'boom',
                        };
                        if ('shellStatus' in c) event.shellStatus = c.shellStatus;
                        window.appendMessageNode(chat, event);
                        const out = chat.querySelector('.shell-output');
                        if (!out) throw new Error('Expected .shell-output node');
                        titles[c.key] = out.textContent.split('\\n')[0];
                    }
                    return titles;
                }""")
            finally:
                browser.close()

        try:
            self.assertEqual(result["exit|true"], "Executed")
            self.assertEqual(result["empty|denied"], "Denied")
            self.assertEqual(result["missing|denied"], "Denied")
            self.assertEqual(result["exit|denied"], "Denied")
            self.assertEqual(result["stopped|false"], "Stopped")
            self.assertEqual(result["timeout|true"], "Timed out")
            self.assertEqual(result["spawn_error|true"], "Failed to start")
            self.assertEqual(result["wait_error|true"], "Failed while waiting")
            self.assertEqual(result["output_read_error|true"], "Failed reading output")
            self.assertEqual(result["drain_timeout|true"], "Output did not finish")
            # Unknown approved states must fail closed, never read as success.
            self.assertEqual(result["future_unknown_status|true"], "Failed")
            self.assertEqual(result["empty|approved"], "Failed")
        except AssertionError as exc:
            raise AssertionError(f"{exc} :: diag={_format_diagnostics(diag)}") from exc

    def test_local_save_failure_keeps_popover_open(self) -> None:
        diag: dict[str, object] = {}
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1024, "height": 768})
                diag = _attach_page_diagnostics(page)
                try:
                    response = _goto_restored_ui(page, self.httpd.launch_url(self.base_url))
                    diag["goto_status"] = response.status if response else None
                except Exception as exc:
                    raise AssertionError(
                        f"homepage/scripts failed :: diag={_format_diagnostics(diag)}"
                    ) from exc
                result = page.evaluate("""async () => {
                    await window.CodeyProviderUI.openLocalConfig();
                    document.getElementById('local-base-url').value = 'http://127.0.0.1:9/v1';
                    document.getElementById('local-model-name').value = 'm';
                    const realFetch = window.fetch.bind(window);
                    window.fetch = (url, opts) => {
                        if (typeof url === 'string' && url.includes('/api/local_provider') && opts && opts.method === 'POST') {
                            return Promise.resolve(new Response(JSON.stringify({ ok: false, error: 'bad budget' }), {
                                status: 400, headers: { 'Content-Type': 'application/json' },
                            }));
                        }
                        return realFetch(url, opts);
                    };
                    document.getElementById('local-config-save').click();
                    await new Promise((r) => setTimeout(r, 300));
                    const pop = document.getElementById('local-config-pop');
                    return {
                        open: pop.open,
                        error: document.getElementById('local-config-error').textContent,
                    };
                }""")
            finally:
                browser.close()

        try:
            self.assertTrue(result["open"])
            self.assertIn("bad budget", result["error"])
        except AssertionError as exc:
            raise AssertionError(f"{exc} :: diag={_format_diagnostics(diag)}") from exc


if __name__ == "__main__":
    unittest.main()
