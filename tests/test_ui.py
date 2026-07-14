from __future__ import annotations

import unittest
from pathlib import Path


HTML = (Path(__file__).resolve().parents[1] / "codey" / "web" / "index.html").read_text(encoding="utf-8")


class ProviderSelectorUiTests(unittest.TestCase):
    def test_provider_selector_lists_supported_providers(self) -> None:
        self.assertIn('id="provider-button"', HTML)
        self.assertIn('id="provider-menu"', HTML)
        self.assertIn('data-provider="deepseek"', HTML)
        self.assertIn('data-provider="qwen"', HTML)
        self.assertIn('data-provider="mimo"', HTML)
        self.assertIn('data-provider="glm"', HTML)
        self.assertIn("glm: 'GLM'", HTML)
        self.assertIn('id="provider-dot"', HTML)
        self.assertIn("--ok-dot:", HTML)
        self.assertIn(".dot.ok", HTML)
        self.assertIn("let providerStatus", HTML)
        self.assertIn("function refreshProviderStatus()", HTML)
        self.assertIn("fetch('/api/providers')", HTML)
        self.assertIn("providerStatus[id] ? 'ok' : ''", HTML)
        self.assertIn(".provider-item.active .check", HTML)
        self.assertNotIn("providerAvailability(_id) { return 'ok'; }", HTML)

    def test_provider_selector_orders_deepseek_mimo_qwen_glm(self) -> None:
        deepseek = HTML.index('data-provider="deepseek"')
        mimo = HTML.index('data-provider="mimo"')
        qwen = HTML.index('data-provider="qwen"')
        glm = HTML.index('data-provider="glm"')

        self.assertLess(deepseek, mimo)
        self.assertLess(mimo, qwen)
        self.assertLess(qwen, glm)

    def test_run_and_continue_requests_keep_session_provider(self) -> None:
        self.assertIn("provider: currentProviderId()", HTML)
        self.assertIn("provider: s.provider || DEFAULT_PROVIDER", HTML)
        self.assertIn("provider: PROVIDERS.includes(s.provider)", HTML)
        self.assertIn("Continue the unfinished task in this same conversation.", HTML)
        self.assertNotIn("Continue the unfinished Codey task", HTML)

    def test_context_handoff_stays_hidden(self) -> None:
        self.assertIn("JSON.stringify({ session_id: s.id })", HTML)
        self.assertNotIn("/compact", HTML)
        self.assertNotIn("context limit", HTML.lower())
        self.assertNotIn("context compression", HTML.lower())
        self.assertNotIn("handoff summary", HTML.lower())

    def test_local_continuity_stays_hidden_and_chat_deletion_cleans_state(self) -> None:
        self.assertIn("async function forgetSessionState(id)", HTML)
        self.assertIn("if (!await forgetSessionState(id)) return;", HTML)
        self.assertIn("await Promise.all", HTML)
        self.assertNotIn("Project Facts", HTML)
        self.assertNotIn("Conversation Snapshot", HTML)
        self.assertNotIn("Durable Snapshot", HTML)
        self.assertNotIn("Recovered", HTML)

    def test_retry_uses_current_session_model_picker(self) -> None:
        retry_start = HTML.index("function retryTask(sessionId)")
        retry_end = HTML.index("function sessionProjectPath", retry_start)
        retry_block = HTML[retry_start:retry_end]
        self.assertIn("syncProviderUI(s.provider || DEFAULT_PROVIDER)", retry_block)
        self.assertIn("$('send').click()", retry_block)
        self.assertIn("provider: currentProviderId()", HTML)

    def test_provider_selector_is_enabled_when_idle(self) -> None:
        self.assertIn("$('provider-button').disabled = busy", HTML)
        self.assertIn("$('provider-button').disabled = false", HTML)
        self.assertNotIn("btn.disabled = !providerStatus", HTML)

    def test_sse_reconnect_reconciles_one_run_snapshot_quietly(self) -> None:
        self.assertIn("function reconcileRunState()", HTML)
        self.assertIn("fetch('/api/state', { cache: 'no-store' })", HTML)
        self.assertIn("runningRunId", HTML)
        self.assertIn("data.pending_event", HTML)
        self.assertIn("data.last_terminal_event", HTML)
        self.assertIn("data.last_shell_result", HTML)
        self.assertIn("markTerminalRun", HTML)
        self.assertIn("if (reconcilePromise) return reconcilePromise", HTML)
        self.assertIn("bufferedServerEvents.push(data)", HTML)
        self.assertIn("for (const event of events) handleServerEvent(event)", HTML)
        self.assertIn("ingestServerEvent(data)", HTML)
        self.assertIn("}, 5000);", HTML)
        self.assertIn("setStatus('Reconnecting…', 'warn')", HTML)
        self.assertNotIn("setStatus('Disconnected', 'err')", HTML)
        start = HTML.index("function applyRunState(data)")
        end = HTML.index("function reconcileRunState()", start)
        block = HTML[start:end]
        self.assertLess(
            block.index("data.last_shell_result"),
            block.index("data.last_terminal_event"),
        )

    def test_shell_approval_applies_http_result_without_waiting_for_sse(self) -> None:
        start = HTML.index("async function approveCommand")
        end = HTML.index("// ============================ composer", start)
        block = HTML[start:end]
        self.assertIn("const data = await r.json()", block)
        self.assertIn("if (data.event) handleServerEvent(data.event)", block)
        self.assertIn("removeShellRequest(sid, data.id)", HTML)
        self.assertIn("function removeShellRequest(sessionId, approvalId)", HTML)

    def test_provider_status_is_quiet_and_refreshes_on_menu_open(self) -> None:
        self.assertIn("if (menu.classList.contains('open')) refreshProviderStatus();", HTML)
        self.assertIn("if (data.type === 'providers')", HTML)
        self.assertIn("applyProviderStatus(data.providers)", HTML)
        self.assertIn("data.provider_failure.action === 'connect'", HTML)
        self.assertIn("providerStatus[data.provider] = false", HTML)
        self.assertIn("addSendError(sid, terminalKey, runId)", HTML)
        self.assertNotIn("Could not connect model", HTML)
        self.assertNotIn("Open the model page", HTML)
        self.assertNotIn("PlaywrightContextManager", HTML)

    def test_deleting_last_session_preserves_selected_provider(self) -> None:
        self.assertIn("const fallbackProvider = currentProviderId()", HTML)
        self.assertIn("provider: fallbackProvider", HTML)

    def test_topbar_shows_running_spinner(self) -> None:
        self.assertIn(".spinner", HTML)
        self.assertIn('id="status"', HTML)
        self.assertIn("setStatus('Running', 'run')", HTML)

    def test_status_rows_use_continue_and_retry_links(self) -> None:
        self.assertIn(".link-btn", HTML)
        self.assertIn("Continue", HTML)
        self.assertIn("Retry", HTML)
        self.assertIn("Resume", HTML)

    def test_control_teaching_uses_plain_paused_status_row(self) -> None:
        self.assertIn("data.type === 'teach_request'", HTML)
        self.assertIn("Click the control in the model page", HTML)
        self.assertIn("resumeTeaching", HTML)
        self.assertIn("/api/teach/resume", HTML)
        teach_start = HTML.index("} else if (m.type === 'teach') {")
        teach_end = HTML.index("} else if (m.type === 'err') {", teach_start)
        teach_block = HTML[teach_start:teach_end]
        self.assertNotIn("DOM", teach_block)
        self.assertNotIn("selector", teach_block.lower())
        self.assertNotIn("override", teach_block.lower())

    def test_project_completion_separates_answer_from_changed_receipt(self) -> None:
        self.assertIn("type: 'asst', text: summary", HTML)
        self.assertIn("const changed = !!data.changed", HTML)
        self.assertIn("changed && changedCount > 0", HTML)
        self.assertIn("data.receipt && data.receipt.text", HTML)
        self.assertIn("text: data.receipt.text", HTML)
        self.assertIn("label: 'View diff'", HTML)
        self.assertIn("onclick: () => openChangesDrawer(m.project)", HTML)
        self.assertIn("let shownReceipt = false", HTML)
        self.assertIn("shownReceipt = true", HTML)
        self.assertIn("data.changed && !shownReceipt", HTML)
        self.assertIn("type: 'changes'", HTML)

    def test_agent_events_are_structured_instead_of_parsed_from_logs(self) -> None:
        self.assertIn("if (data.type === 'turn')", HTML)
        self.assertIn("if (data.type === 'tool')", HTML)
        self.assertIn("if (data.type === 'info')", HTML)
        self.assertNotIn("const turnMatch = line.match", HTML)
        self.assertNotIn("const toolMatch = line.match", HTML)
        self.assertNotIn("if (data.type === 'log')", HTML)
        self.assertNotIn("function handleLog", HTML)

    def test_send_failures_render_inline_error(self) -> None:
        self.assertIn("function addSendError(sessionId, eventKey = '', runId = '')", HTML)
        self.assertIn("Could not send the message", HTML)
        self.assertIn("if (r.status === 409) { addSendError(activeId); return; }", HTML)
        self.assertIn("if (r.status === 409 || !r.ok) {", HTML)
        self.assertIn("await acceptRunResponse(r, sessionId)", HTML)
        self.assertIn("action: { label: 'Retry'", HTML)
        self.assertNotIn("Switch provider", HTML)
        self.assertNotIn("Switch model", HTML)
        self.assertNotIn('title="Provider"', HTML)
        self.assertIn('title="Model"', HTML)
        err_start = HTML.index("} else if (m.type === 'err') {")
        err_end = HTML.index("} else if (m.type === 'info') {", err_start)
        err_block = HTML[err_start:err_end]
        self.assertIn("Retry", err_block)
        self.assertNotIn("Continue", err_block)
        self.assertNotIn("alert('Failed to start:", HTML)
        self.assertNotIn("alert('Failed to continue:", HTML)
        self.assertNotIn("alert('A task is already running')", HTML)

    def test_composer_enter_sends_and_shift_enter_keeps_newline(self) -> None:
        self.assertIn('id="send-hint">Enter</span>', HTML)
        self.assertIn("e.key === 'Enter' && !e.shiftKey", HTML)
        self.assertIn("!e.isComposing", HTML)
        self.assertIn("e.keyCode !== 229", HTML)
        self.assertNotIn("e.key === 'Enter' && (e.ctrlKey || e.metaKey)", HTML)
        self.assertNotIn('id="send-hint">Ctrl ↵</span>', HTML)

    def test_routine_failures_do_not_use_native_alerts(self) -> None:
        self.assertNotIn("alert(", HTML)
        self.assertIn("setStatus('Could not add project', 'err')", HTML)
        self.assertIn("Command approval", HTML)
        self.assertIn("Could not send approval", HTML)
        self.assertIn("function showCommandApprovalError", HTML)
        self.assertNotIn("Failed to pick folder", HTML)
        self.assertNotIn("Shell approval failed", HTML)
        self.assertNotIn("approveShell", HTML)

    def test_review_status_is_quiet_and_has_no_switch(self) -> None:
        self.assertIn("data.type === 'review'", HTML)
        self.assertIn("{ type: 'review', text: data.text, runId }", HTML)
        self.assertIn("statusRow('Review'", HTML)
        self.assertNotIn("Review mode", HTML)

    def test_consensus_has_no_visible_ui_mode(self) -> None:
        self.assertNotIn("MoA", HTML)
        self.assertNotIn("Consensus", HTML)
        self.assertNotIn("Ask all", HTML)
        self.assertNotIn("multi-model", HTML)
        self.assertNotIn("group chat", HTML)
        self.assertNotIn("cowork", HTML)
        self.assertNotIn("Switch provider", HTML)

    def test_changes_drawer_supports_snapshot_mode_and_restore(self) -> None:
        self.assertIn('id="changes-restore"', HTML)
        self.assertIn("Reading changes", HTML)
        self.assertIn("data.mode === 'git' ? 'Git' : 'Snapshot'", HTML)
        self.assertIn("/api/changes/restore", HTML)
        self.assertNotIn("Reading git diff", HTML)

    def test_changes_drawer_hides_diff_metadata_lines(self) -> None:
        self.assertIn('class="diff-line add"><span class="ln"', HTML)
        self.assertIn('class="diff-line del"><span class="ln"', HTML)
        self.assertIn("line.startsWith('diff --git')", HTML)
        self.assertNotIn("line.startsWith('@@')) cls += ' hunk'", HTML)

    def test_composer_holds_model_picker_with_green_dot(self) -> None:
        self.assertIn('class="provider-chooser"', HTML)
        self.assertIn('class="provider-button"', HTML)
        self.assertIn('class="provider-menu"', HTML)
        self.assertIn('class="provider-item"', HTML)

    def test_send_and_stop_share_one_action_slot(self) -> None:
        self.assertIn('class="action-slot"', HTML)
        slot_start = HTML.index('<div class="action-slot">')
        slot_end = HTML.index('</div>', slot_start)
        slot = HTML[slot_start:slot_end]
        self.assertIn('id="stop"', slot)
        self.assertIn('id="send"', slot)
        update_start = HTML.index("function updateSend()")
        update_end = HTML.index("$('task').addEventListener", update_start)
        update_block = HTML[update_start:update_end]
        self.assertIn("$('send').style.display = running ? 'none' : ''", update_block)
        self.assertIn("$('stop').style.display = running ? '' : 'none'", update_block)
        self.assertIn("$('send-hint').textContent = running ? 'Stop' : 'Enter'", update_block)
        self.assertNotIn("$('send-hint').style.display", update_block)

    def test_welcome_keeps_status_copy_without_example_cards(self) -> None:
        self.assertIn("Send a message to start.", HTML)
        self.assertIn("<h1>Codey</h1>", HTML)
        self.assertNotIn("打个招呼", HTML)
        self.assertNotIn("生成 snake.py", HTML)
        self.assertNotIn("写 README", HTML)
        self.assertNotIn("找 bug", HTML)

    def test_chat_messages_have_quiet_copy_button(self) -> None:
        self.assertIn("function addMessageCopyButton(div, text)", HTML)
        self.assertIn("function copyText(text)", HTML)
        self.assertIn("navigator.clipboard.writeText(value)", HTML)
        self.assertIn("document.execCommand('copy')", HTML)
        self.assertIn("className = 'msg-copy'", HTML)
        self.assertIn("opacity: .45", HTML)
        self.assertIn(".msg:hover .msg-copy", HTML)
        self.assertIn(".msg:focus-within .msg-copy", HTML)
        self.assertIn("aria-label', 'Copy message'", HTML)
        self.assertIn("addMessageCopyButton(div, messageCopyText(m))", HTML)
        self.assertIn("copyText: text", HTML)
        self.assertNotIn("Export chat", HTML)

    def test_chat_messages_and_titles_remain_local_persistent(self) -> None:
        self.assertIn("const LS_SESSIONS = 'codey:sessions';", HTML)
        self.assertIn("messages: Array.isArray(s.messages) ? s.messages : []", HTML)
        self.assertIn("title: s.title || 'New chat'", HTML)
        self.assertIn("function saveSessions(arr)", HTML)
        self.assertIn("localStorage.setItem(LS_SESSIONS, JSON.stringify(arr))", HTML)
        self.assertIn("s.title = m.text.slice(0, 28)", HTML)
        self.assertIn("s.title = title.slice(0, 80)", HTML)

    def test_chat_state_uses_backend_snapshot_with_local_cache(self) -> None:
        self.assertIn("function currentUiState()", HTML)
        self.assertIn("function cacheUiState()", HTML)
        self.assertIn("function saveUiStateToServer()", HTML)
        self.assertIn("async function restoreUiStateFromServer()", HTML)
        self.assertIn("const LS_UI_REVISION = 'codey:ui-revision';", HTML)
        self.assertIn("revision: uiStateRevision", HTML)
        self.assertIn("function isUiStateNewer(a, b)", HTML)
        self.assertIn("uiStateRevision += 1", HTML)
        self.assertIn("function hasMeaningfulUiState(state)", HTML)
        self.assertIn("const bootHadMeaningfulUiState = hasMeaningfulUiState(bootCachedState)", HTML)
        self.assertIn("fetch('/api/ui_state'", HTML)
        self.assertIn("method: 'POST'", HTML)
        self.assertIn("body: JSON.stringify({ state: currentUiState() })", HTML)
        self.assertIn("navigator.sendBeacon('/api/ui_state'", HTML)
        self.assertIn("function connectEvents()", HTML)
        self.assertIn("await restoreUiStateFromServer();", HTML)
        self.assertIn("connectEvents();", HTML)
        self.assertLess(HTML.index("await restoreUiStateFromServer();"), HTML.index("connectEvents();"))
        self.assertIn("persistActive();", HTML[HTML.index("function ensureProject"):HTML.index("async function pickProject")])

    def test_assistant_replies_render_minimal_markdown(self) -> None:
        self.assertIn("function renderMarkdown(container, text)", HTML)
        self.assertIn("function renderInlineMd(text)", HTML)
        self.assertIn("const escaped = escapeHtml(text);", HTML)
        self.assertIn("<strong>$1</strong>", HTML)
        self.assertIn("body.className = 'body md collapsed'", HTML)
        self.assertIn("renderMarkdown(body, m.text)", HTML)
        self.assertNotIn("body.textContent = m.text;", HTML)
        self.assertIn(".md-code", HTML)
        self.assertIn(".md-ic", HTML)
        self.assertIn(".md-list", HTML)
        self.assertIn("background: var(--panel-2)", HTML)

    def test_inline_code_spans_are_not_bolded(self) -> None:
        self.assertIn("function applyBold(segment)", HTML)
        self.assertIn("out += applyBold(escaped.slice(last, m.index));", HTML)
        self.assertIn("${m[1]}</code>", HTML)
        self.assertIn("out += applyBold(escaped.slice(last));", HTML)

    def test_markdown_stays_monochrome_without_syntax_highlighting(self) -> None:
        md_css_start = HTML.index("assistant markdown")
        md_css_end = HTML.index("tool line", md_css_start)
        md_css = HTML[md_css_start:md_css_end]
        self.assertNotIn("#", md_css.replace("var(--", ""))
        self.assertNotIn("hljs", HTML)
        self.assertNotIn("highlight.js", HTML)

    def test_code_blocks_have_quiet_copy_button(self) -> None:
        self.assertIn("function addCodeCopyButton(pre, text)", HTML)
        self.assertIn("function appendCodeBlock(container, code)", HTML)
        self.assertIn("className = 'code-copy'", HTML)
        self.assertIn("aria-label', 'Copy code'", HTML)
        self.assertIn("el.textContent = code", HTML)
        self.assertIn(".md-code:hover .code-copy", HTML)
        self.assertIn("await copyText(value)", HTML)


if __name__ == "__main__":
    unittest.main()
