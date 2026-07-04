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

    def test_done_receipt_can_open_changes_without_duplicate_summary(self) -> None:
        self.assertIn("data.receipt && data.receipt.text", HTML)
        self.assertIn("text: data.receipt.text", HTML)
        self.assertIn("label: 'View diff'", HTML)
        self.assertIn("onclick: () => openChangesDrawer(m.project)", HTML)
        self.assertIn("let shownReceipt = false", HTML)
        self.assertIn("shownReceipt = true", HTML)
        self.assertIn("!shownReceipt) maybeAddChangesSummary(sid, terminalKey, runId)", HTML)
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

    def test_welcome_keeps_status_copy_without_example_cards(self) -> None:
        self.assertIn("Send a message to start.", HTML)
        self.assertIn("<h1>Codey</h1>", HTML)
        self.assertNotIn("打个招呼", HTML)
        self.assertNotIn("生成 snake.py", HTML)
        self.assertNotIn("写 README", HTML)
        self.assertNotIn("找 bug", HTML)


if __name__ == "__main__":
    unittest.main()
