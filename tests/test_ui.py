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

    def test_shell_approval_renders_risk_explanation(self) -> None:
        self.assertIn("title.textContent = 'Approval required'", HTML)
        self.assertIn("className = 'sc-note'", HTML)
        self.assertIn("${riskTitle || 'Shell command'}", HTML)
        self.assertIn("riskLabel: data.risk_label", HTML)
        self.assertIn("riskTitle: data.risk_title", HTML)
        self.assertIn("riskDetail: data.risk_detail", HTML)

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

    def test_tool_start_pending_row_is_replaced_by_final_tool(self) -> None:
        self.assertIn(".tool-line.pending .tl-result", HTML)
        self.assertIn("if (m.pending) row.classList.add('pending');", HTML)
        self.assertIn(
            "result.textContent = m.pending ? (m.activity || m.result || 'Working') : (m.result || '');",
            HTML,
        )

        pending_render_start = HTML.index("} else if (m.type === 'tool_pending') {")
        pending_render_end = HTML.index("} else if (m.type === 'done') {", pending_render_start)
        pending_render_block = HTML[pending_render_start:pending_render_end]
        self.assertIn("chat.appendChild(standaloneToolEl(m));", pending_render_block)

        started_start = HTML.index("if (data.type === 'tool_started')")
        started_end = HTML.index("if (data.type === 'tool')", started_start)
        started_block = HTML[started_start:started_end]
        self.assertIn("const rawToolId = (data.tool_id || '').toString();", started_block)
        self.assertIn("const toolKey = rawToolId ? `${runId}:${rawToolId}` : '';", started_block)
        self.assertIn("s.messages.some(item => item.toolKey === toolKey)", started_block)
        self.assertIn("type: 'tool_pending'", started_block)
        self.assertIn("pending: true", started_block)
        self.assertIn("activity: (data.activity || '').toString().slice(0, 200)", started_block)

        final_start = HTML.index("if (data.type === 'tool')", started_end)
        final_end = HTML.index("if (data.type === 'info')", final_start)
        final_block = HTML[final_start:final_end]
        self.assertIn("const rawToolId = (data.tool_id || '').toString();", final_block)
        self.assertIn("const toolKey = rawToolId ? `${runId}:${rawToolId}` : '';", final_block)
        self.assertIn("replaceSessionMessage(", final_block)
        self.assertIn("item => item.type === 'tool_pending' && item.toolKey === toolKey", final_block)
        self.assertIn("addToSession(sid, message);", final_block)

        replace_start = HTML.index("function replaceSessionMessage")
        replace_end = HTML.index("function markTerminalRun", replace_start)
        replace_block = HTML[replace_start:replace_end]
        self.assertIn("const index = s.messages.findIndex(predicate);", replace_block)
        self.assertIn("s.messages[index] = message;", replace_block)
        self.assertIn("renderChat(); scrollChat();", replace_block)

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
        self.assertIn("Approval required", HTML)
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

    def test_ui_state_persistence_is_debounced_with_immediate_flush_helpers(self) -> None:
        # A-1: hot-path persistence is coalesced behind a debounce timer, while
        # discrete/terminal moments flush immediately. Data shape is unchanged.
        self.assertIn("function markUiStateDirty()", HTML)
        self.assertIn("function flushUiState()", HTML)
        self.assertIn("function persistActive()", HTML)
        self.assertIn("function persistActiveNow()", HTML)
        self.assertIn("let uiStatePersistTimer = null;", HTML)
        self.assertIn("const UI_STATE_PERSIST_DELAY = 400;", HTML)
        # revision bump preserved (relied on by isUiStateNewer ordering).
        self.assertIn("uiStateRevision += 1", HTML)

        dirty_start = HTML.index("function markUiStateDirty()")
        dirty_end = HTML.index("function flushUiState()", dirty_start)
        dirty_block = HTML[dirty_start:dirty_end]
        self.assertIn("uiStateUpdatedAt = Math.max(Date.now(), uiStateUpdatedAt);", dirty_block)
        self.assertIn("uiStateRevision += 1;", dirty_block)
        self.assertIn("uiStateDirtySinceBoot = true;", dirty_block)

        flush_start = HTML.index("function flushUiState()")
        flush_end = HTML.index("function persistActive()", flush_start)
        flush_block = HTML[flush_start:flush_end]
        self.assertIn("clearTimeout(uiStatePersistTimer)", flush_block)
        self.assertIn("cacheUiState();", flush_block)
        self.assertIn("saveUiStateToServer();", flush_block)

        persist_start = HTML.index("function persistActive()")
        persist_end = HTML.index("function persistActiveNow()", persist_start)
        persist_block = HTML[persist_start:persist_end]
        self.assertIn("markUiStateDirty();", persist_block)
        self.assertIn("if (uiStatePersistTimer !== null) return;", persist_block)
        self.assertIn("uiStatePersistTimer = setTimeout(", persist_block)
        self.assertIn("UI_STATE_PERSIST_DELAY", persist_block)

        now_start = HTML.index("function persistActiveNow()")
        now_end = HTML.index("function updateComposerContext()", now_start)
        now_block = HTML[now_start:now_end]
        self.assertIn("markUiStateDirty();", now_block)
        self.assertIn("flushUiState();", now_block)

    def test_streaming_events_stay_debounced_while_user_actions_flush(self) -> None:
        # A-1: the SSE hot path (addToSession) must keep the debounced persistActive,
        # otherwise every turn/tool/info event would序列化+落盘 all over again.
        add_start = HTML.index("function addToSession(sid, m)")
        add_end = HTML.index("function markTerminalRun", add_start)
        add_block = HTML[add_start:add_end]
        self.assertIn("persistActive();", add_block)
        self.assertNotIn("persistActiveNow(", add_block)

        # User-visible / discrete mutations flush immediately.
        push_start = HTML.index("function pushMsg(m)")
        push_end = HTML.index("function openMenuAt", push_start)
        push_block = HTML[push_start:push_end]
        self.assertIn("persistActiveNow();", push_block)
        self.assertNotIn("persistActive();", push_block)

        # Terminal task events flush the coalesced state right away.
        done_start = HTML.index("if (data.type === 'task_done')")
        done_end = HTML.index("if (data.type === 'shell_request')", done_start)
        done_block = HTML[done_start:done_end]
        self.assertIn("flushUiState();", done_block)

    def test_discrete_session_actions_flush_immediately(self) -> None:
        # A-1 follow-up: switching/creating a chat and picking a provider are
        # discrete user actions, not the SSE hot path, so they flush now.
        switch_start = HTML.index("function switchSession(id)")
        switch_end = HTML.index("function openProject(projectId)", switch_start)
        self.assertIn("persistActiveNow();", HTML[switch_start:switch_end])

        new_start = HTML.index("function newSession(projectId = null)")
        new_end = HTML.index("async function forgetSessionState(id)", new_start)
        self.assertIn("persistActiveNow();", HTML[new_start:new_end])

        prov_start = HTML.index("function setActiveProvider(id)")
        prov_end = HTML.index("$('provider-button').onclick", prov_start)
        self.assertIn("persistActiveNow();", HTML[prov_start:prov_end])

        # The debounced path is still used where coalescing matters: the SSE hot
        # path (addToSession) and low-signal toggles like ensureProject / expand.
        self.assertIn(
            "persistActive();",
            HTML[HTML.index("function ensureProject"):HTML.index("async function pickProject")],
        )

    def test_pagehide_flushes_pending_ui_state_before_beacon(self) -> None:
        page_start = HTML.index("window.addEventListener('pagehide'")
        page_end = HTML.index("async function boot()", page_start)
        page_block = HTML[page_start:page_end]
        self.assertIn("clearTimeout(uiStatePersistTimer)", page_block)
        self.assertIn("cacheUiState();", page_block)
        self.assertIn("navigator.sendBeacon('/api/ui_state'", page_block)
        self.assertLess(
            page_block.index("cacheUiState();"),
            page_block.index("navigator.sendBeacon('/api/ui_state'"),
        )

    def test_rename_uses_inline_input_not_native_prompt(self) -> None:
        # B-1: rename is an ordinary edit -> inline <input>, no native prompt().
        self.assertNotIn("prompt(", HTML)
        self.assertIn(".rename-input", HTML)
        self.assertIn("function renameInputEl(value, commit, cancel)", HTML)
        self.assertIn("function focusRenameInput()", HTML)
        self.assertIn("let editingSessionId = '';", HTML)
        self.assertIn("let editingProjectId = '';", HTML)
        self.assertIn("function commitSessionRename(id, value)", HTML)
        self.assertIn("function commitProjectRename(id, value)", HTML)
        self.assertIn("function cancelSessionRename()", HTML)
        self.assertIn("function cancelProjectRename()", HTML)

        # Enter commits, Escape cancels, blur commits (avoids losing the edit).
        input_start = HTML.index("function renameInputEl(value, commit, cancel)")
        input_end = HTML.index("function focusRenameInput()", input_start)
        input_block = HTML[input_start:input_end]
        self.assertIn("e.key === 'Enter'", input_block)
        self.assertIn("finish(commit, input.value)", input_block)
        self.assertIn("e.key === 'Escape'", input_block)
        self.assertIn("finish(cancel)", input_block)
        self.assertIn("input.onblur = () => finish(commit, input.value);", input_block)
        # settled guard prevents a double commit from cancel->render->blur.
        self.assertIn("let settled = false;", input_block)
        # clicks inside the input must not bubble to the row switch/open handlers.
        self.assertIn("input.onmousedown = (e) => e.stopPropagation();", input_block)

        # rename entry points arm inline editing rather than prompting.
        rs_start = HTML.index("function renameSession(id)")
        rs_end = HTML.index("async function clearMessages(id)", rs_start)
        rs_block = HTML[rs_start:rs_end]
        self.assertIn("editingSessionId = id;", rs_block)
        self.assertIn("focusRenameInput();", rs_block)
        rp_start = HTML.index("function renameProject(id)")
        rp_end = HTML.index("async function deleteProject(id)", rp_start)
        rp_block = HTML[rp_start:rp_end]
        self.assertIn("editingProjectId = id;", rp_block)
        self.assertIn("focusRenameInput();", rp_block)
        # committing a session rename still clamps title length (unchanged shape).
        self.assertIn("s.title = title.slice(0, 80);", HTML)

    def test_destructive_actions_use_two_step_arm_not_native_confirm(self) -> None:
        # B-1: delete/clear are destructive -> in-menu two-step confirm, no confirm().
        self.assertNotIn("confirm(", HTML)
        self.assertIn("function armDanger(btn, run)", HTML)
        self.assertIn("function disarmDangerButtons()", HTML)
        self.assertIn(".ctx-menu button.arming", HTML)
        # auto-revert after a few seconds keeps the menu quiet.
        self.assertIn("setTimeout(disarmDangerButtons, 3000)", HTML)

        # every destructive menu item carries its confirm label.
        self.assertIn('data-act="remove" class="danger" data-confirm="Confirm remove"', HTML)
        self.assertIn('data-act="delete" class="danger" data-confirm="Confirm delete"', HTML)
        self.assertIn('data-act="clear" class="danger" data-confirm="Confirm clear"', HTML)
        # the project remove label must state that chats are deleted, not merely
        # "removed from sidebar" (deleteProject drops sessions under the project).
        self.assertIn('>Remove project and chats</button>', HTML)
        self.assertNotIn('Remove from sidebar', HTML)

        # first click arms + relabels; second click runs and closes.
        arm_start = HTML.index("function armDanger(btn, run)")
        arm_end = HTML.index("// ============================ chat rendering", arm_start)
        arm_block = HTML[arm_start:arm_end]
        self.assertIn("if (btn.classList.contains('arming'))", arm_block)
        self.assertIn("run();", arm_block)
        self.assertIn("btn.textContent = btn.dataset.confirm", arm_block)
        self.assertIn("btn.classList.add('arming');", arm_block)

        # closing any menu disarms pending confirmations.
        close_start = HTML.index("function closeAllMenus()")
        close_end = HTML.index("function openProjectMenu", close_start)
        close_block = HTML[close_start:close_end]
        self.assertIn("disarmDangerButtons();", close_block)

        # menu handlers route destructive acts through armDanger and return early.
        self.assertIn("if (act === 'remove') { if (id) armDanger(btn, () => deleteProject(id)); return; }", HTML)
        self.assertIn("if (act === 'delete') { if (id) armDanger(btn, () => deleteSession(id)); return; }", HTML)
        self.assertIn("if (act === 'clear') { armDanger(btn, () => clearMessages(activeId)); return; }", HTML)

        # the destructive ops themselves no longer gate on a native confirm.
        del_start = HTML.index("async function deleteSession(id)")
        del_end = HTML.index("function renameSession(id)", del_start)
        del_block = HTML[del_start:del_end]
        self.assertIn("if (!await forgetSessionState(id)) return;", del_block)

    def test_readonly_tool_lines_fold_into_render_layer_groups(self) -> None:
        # B-2: only consecutive read-only tools fold; grouping is render-only and
        # never touches the sessions data structure.
        self.assertIn(
            "const FOLDABLE_TOOL_KINDS = new Set(['read', 'ls', 'search', 'references']);",
            HTML,
        )
        self.assertIn("function toolRowEl(m, compact)", HTML)
        self.assertIn("function createToolGroup(kind)", HTML)
        self.assertIn("function standaloneToolEl(m)", HTML)
        self.assertIn("function appendToToolGroup(group, m)", HTML)
        self.assertIn("function appendOrFoldTool(chat, m)", HTML)
        self.assertIn("function foldCountLabel(kind, n)", HTML)

        # count labels pluralize correctly, incl. the irregular "searches".
        self.assertIn("search: ['search', 'searches'],", HTML)
        self.assertIn("read: ['file', 'files'],", HTML)
        self.assertNotIn("noun + (n === 1 ? '' : 's')", HTML)

        # edit / run / shell are deliberately absent from the foldable set.
        set_line = "const FOLDABLE_TOOL_KINDS = new Set(['read', 'ls', 'search', 'references']);"
        self.assertNotIn("'edit'", set_line)
        self.assertNotIn("'run'", set_line)
        self.assertNotIn("'shell'", set_line)

        # the tool branch folds read-only, non-error rows and keeps the rest as
        # standalone tool lines (edit/run/shell/error stay visible).
        tool_start = HTML.index("} else if (m.type === 'tool') {")
        tool_end = HTML.index("} else if (m.type === 'done') {", tool_start)
        tool_block = HTML[tool_start:tool_end]
        self.assertIn("if (FOLDABLE_TOOL_KINDS.has(m.kind) && !m.error) {", tool_block)
        self.assertIn("appendOrFoldTool(chat, m);", tool_block)
        self.assertIn("chat.appendChild(standaloneToolEl(m));", tool_block)

        # a single foldable tool stays visible; only the second consecutive tool
        # of the same kind converts the previous standalone row into a group.
        standalone_start = HTML.index("function standaloneToolEl(m)")
        standalone_end = HTML.index("function appendToToolGroup", standalone_start)
        standalone_block = HTML[standalone_start:standalone_end]
        self.assertIn("div.className = 'msg tool';", standalone_block)
        self.assertIn("div.dataset.foldkind = m.kind;", standalone_block)
        self.assertIn("div.appendChild(toolRowEl(m, false));", standalone_block)

        # merge only into a trailing group or standalone row of the same kind.
        merge_start = HTML.index("function appendOrFoldTool(chat, m)")
        merge_end = HTML.index("function appendMessageNode(chat, m)", merge_start)
        merge_block = HTML[merge_start:merge_end]
        self.assertIn("const last = chat.lastElementChild;", merge_block)
        self.assertIn("last.dataset.foldkind === m.kind", merge_block)
        self.assertIn("last.replaceWith(group);", merge_block)
        self.assertIn("chat.appendChild(standaloneToolEl(m));", merge_block)

        append_start = HTML.index("function appendToToolGroup(group, m)")
        append_end = HTML.index("function appendOrFoldTool(chat, m)", append_start)
        append_block = HTML[append_start:append_end]
        self.assertIn("body.children.length", append_block)

        # converted groups default collapsed and toggle on click; state is not persisted.
        group_start = HTML.index("function createToolGroup(kind)")
        group_end = HTML.index("function standaloneToolEl(m)", group_start)
        group_block = HTML[group_start:group_end]
        self.assertIn("group.className = 'tool-group collapsed';", group_block)
        self.assertIn("group.dataset.foldkind = kind;", group_block)
        self.assertIn("summary.onclick = () => group.classList.toggle('collapsed');", group_block)
        self.assertNotIn("persist", group_block)

        # monochrome, cardless folding CSS: hidden body, chevron rotate, no colors.
        self.assertIn(".tool-line.compact { grid-template-columns: 1fr auto auto; }", HTML)
        self.assertIn(".tool-group-body { display: none; padding-left: 20px; }", HTML)
        self.assertIn(".tool-group:not(.collapsed) .tool-group-body { display: block; }", HTML)
        self.assertIn(".tool-group:not(.collapsed) .tg-chevron { transform: rotate(90deg); }", HTML)
        group_css_start = HTML.index("/* ---------- tool group (read-only folding) ---------- */")
        group_css_end = HTML.index("/* ---------- turn divider ---------- */", group_css_start)
        group_css = HTML[group_css_start:group_css_end]
        self.assertNotIn("#", group_css.replace("var(--", ""))


if __name__ == "__main__":
    unittest.main()
