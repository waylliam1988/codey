from __future__ import annotations

import unittest
from pathlib import Path


WEB_DIR = Path(__file__).resolve().parents[1] / "codey" / "web"
ASSET_DIR = WEB_DIR / "assets"
HTML = (WEB_DIR / "index.html").read_text(encoding="utf-8")
GRAPH_JS = (ASSET_DIR / "research_graph.js").read_text(encoding="utf-8")
RESEARCH_DRAWER_JS = (ASSET_DIR / "research_drawer.js").read_text(encoding="utf-8")
CHANGES_DRAWER_JS = (ASSET_DIR / "changes_drawer.js").read_text(encoding="utf-8")
LOCAL_CONTEXT_DRAWER_JS = (ASSET_DIR / "local_context_drawer.js").read_text(encoding="utf-8")
RUN_DETAILS_JS = (ASSET_DIR / "run_details.js").read_text(encoding="utf-8")
RENDER_JS = (ASSET_DIR / "render.js").read_text(encoding="utf-8")
PROVIDER_UI_JS = (ASSET_DIR / "provider_ui.js").read_text(encoding="utf-8")
UI_STATE_JS = (ASSET_DIR / "ui_state.js").read_text(encoding="utf-8")
SSE_JS = (ASSET_DIR / "sse.js").read_text(encoding="utf-8")
COMPOSER_JS = (ASSET_DIR / "composer.js").read_text(encoding="utf-8")
TOKENS_CSS = (ASSET_DIR / "tokens.css").read_text(encoding="utf-8")
APP_CSS = (ASSET_DIR / "app.css").read_text(encoding="utf-8")
STYLE_SOURCE = TOKENS_CSS + "\n" + APP_CSS
JS_ASSETS = "\n".join(
    path.read_text(encoding="utf-8") for path in sorted(ASSET_DIR.glob("*.js"))
)
UI_SOURCE = HTML + "\n" + STYLE_SOURCE + "\n" + JS_ASSETS


class ProviderSelectorUiTests(unittest.TestCase):
    def test_visible_ui_uses_english_document_language(self) -> None:
        self.assertIn('<html lang="en">', HTML)
        self.assertNotRegex(UI_SOURCE, r"[\u4e00-\u9fff]")

    def test_web_colors_stay_in_tokens(self) -> None:
        non_token_ui = HTML + "\n" + APP_CSS + "\n" + JS_ASSETS
        self.assertRegex(TOKENS_CSS, r"#[0-9a-fA-F]{3,6}")
        self.assertNotRegex(non_token_ui, r"#[0-9a-fA-F]{3,6}")
        self.assertIn("--scrollbar-thumb:", TOKENS_CSS)
        self.assertIn("background: var(--scrollbar-thumb)", APP_CSS)
        self.assertIn("color: var(--err-text)", APP_CSS)

    def test_provider_selector_lists_supported_providers(self) -> None:
        self.assertIn('id="provider-button"', HTML)
        self.assertIn('id="provider-menu"', HTML)
        self.assertIn('id="local-config-pop"', HTML)
        self.assertIn("blank keeps saved key", HTML)
        self.assertNotIn("local-clear-api-key", HTML)
        self.assertNotIn("Clear saved key", HTML)
        self.assertNotIn("clear_api_key", HTML)
        self.assertIn("fetch('/api/local_provider')", PROVIDER_UI_JS)
        self.assertIn('data-provider="deepseek"', HTML)
        self.assertIn('data-provider="mimo"', HTML)
        self.assertIn('data-provider="qwen"', HTML)
        self.assertIn('data-provider="stepfun"', HTML)
        self.assertIn('data-provider="glm"', HTML)
        self.assertIn('data-provider="local"', HTML)
        self.assertIn("glm: 'GLM'", UI_STATE_JS)
        self.assertIn("local: 'Local'", UI_STATE_JS)
        self.assertIn('id="provider-dot"', HTML)
        self.assertIn("--ok-dot:", STYLE_SOURCE)
        self.assertIn(".dot.ok", STYLE_SOURCE)
        self.assertIn("let providerStatus", PROVIDER_UI_JS)
        self.assertIn("function refreshProviderStatus(immediate = false)", PROVIDER_UI_JS)
        self.assertIn("fetch('/api/providers')", PROVIDER_UI_JS)
        self.assertIn("providerStatus[id] ? 'ok' : ''", PROVIDER_UI_JS)
        self.assertIn(".provider-item.active .check", STYLE_SOURCE)
        self.assertNotIn("providerAvailability(_id) { return 'ok'; }", UI_SOURCE)
        self.assertIn('<script src="/assets/provider_ui.js?v=__CODEY_VERSION__"></script>', HTML)
        self.assertIn('<script src="/assets/ui_state.js?v=__CODEY_VERSION__"></script>', HTML)
        self.assertIn('<script src="/assets/composer.js?v=__CODEY_VERSION__"></script>', HTML)
        self.assertIn("window.CodeyProviderUI.init({", HTML)
        self.assertIn("window.CodeyProviderUI = {", PROVIDER_UI_JS)
        self.assertIn("function applyProviderStatus(providers, isSSE = true)", HTML)
        self.assertIn("window.CodeyProviderUI.applyStatus(providers, isSSE)", HTML)
        self.assertIn("function refreshProviderStatus(immediate = false)", HTML)
        self.assertIn("window.CodeyProviderUI.refreshStatus(immediate)", HTML)
        self.assertIn('<script src="/assets/sse.js?v=__CODEY_VERSION__"></script>', HTML)
        self.assertIn("window.CodeySse = {", SSE_JS)
        self.assertIn("window.CodeyComposer = {", COMPOSER_JS)

    def test_provider_selector_orders_deepseek_mimo_stepfun_qwen_glm_local(self) -> None:
        deepseek = HTML.index('data-provider="deepseek"')
        mimo = HTML.index('data-provider="mimo"')
        stepfun = HTML.index('data-provider="stepfun"')
        qwen = HTML.index('data-provider="qwen"')
        glm = HTML.index('data-provider="glm"')
        local = HTML.index('data-provider="local"')

        self.assertLess(deepseek, mimo)
        self.assertLess(mimo, stepfun)
        self.assertLess(stepfun, qwen)
        self.assertLess(qwen, glm)
        self.assertLess(glm, local)

    def test_run_and_continue_requests_keep_session_provider(self) -> None:
        self.assertIn("window.CodeyComposer.init({", HTML)
        self.assertIn("await sendTaskFromSession(sessionId, task, provider, () => clearDraftIfUnchanged(sessionId, task));", COMPOSER_JS)
        send_click = COMPOSER_JS[COMPOSER_JS.index("async function sendActiveDraft()"):COMPOSER_JS.index("async function continueTask")]
        self.assertIn("const provider = currentProviderId();", send_click)
        self.assertIn("provider: s.provider || DEFAULT_PROVIDER", COMPOSER_JS)
        self.assertIn("intent: 'project'", COMPOSER_JS)
        self.assertIn("provider: PROVIDERS.includes(s.provider)", UI_STATE_JS)
        self.assertIn("Continue the unfinished task in this same conversation.", COMPOSER_JS)
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
        self.assertNotIn("ghost_sleep", UI_SOURCE)
        self.assertNotIn("ghost_router", UI_SOURCE)
        self.assertNotIn("Cognitive Sleep", UI_SOURCE)
        self.assertNotIn("Ghost Router", UI_SOURCE)

    def test_local_context_entry_is_quiet_top_menu_item(self) -> None:
        rename = HTML.index('<button data-act="rename">Rename chat</button>')
        local = HTML.index('<button data-act="local-context">Local context</button>')
        clear = HTML.index('<button data-act="clear" class="danger"')

        self.assertLess(rename, local)
        self.assertLess(local, clear)
        self.assertIn('id="local-context-drawer"', HTML)
        self.assertIn('<strong>Local context</strong>', HTML)
        self.assertIn('<script src="/assets/local_context_drawer.js?v=__CODEY_VERSION__"></script>', HTML)
        self.assertIn("window.CodeyLocalContextDrawer.init({", HTML)
        self.assertIn("window.CodeyLocalContextDrawer = {", LOCAL_CONTEXT_DRAWER_JS)
        self.assertNotIn("sidebar", LOCAL_CONTEXT_DRAWER_JS.casefold())
        self.assertNotIn("Review context", UI_SOURCE)

    def test_local_context_ui_avoids_internal_terms(self) -> None:
        visible_source = HTML + "\n" + LOCAL_CONTEXT_DRAWER_JS
        for term in ("Ghost", "Memory", "Affinity", "Hebbian", "Directive"):
            self.assertNotIn(term, visible_source)
        self.assertIn("Local context", visible_source)
        self.assertIn("Recent focus", visible_source)
        self.assertIn("Active preferences", visible_source)
        self.assertIn("Local ordering", visible_source)

    def test_run_details_is_quiet_inline_entry_not_new_drawer(self) -> None:
        self.assertIn('<script src="/assets/run_details.js?v=__CODEY_VERSION__"></script>', HTML)
        self.assertIn("window.CodeyRunDetails.init({", HTML)
        self.assertIn("actions: window.CodeyRunDetails.actionsForMessage(m, [action])", HTML)
        self.assertIn("function actionForMessage(message)", RUN_DETAILS_JS)
        self.assertIn("fetch('/api/run_details?'", RUN_DETAILS_JS)
        self.assertIn("panel.className = 'run-details';", RUN_DETAILS_JS)
        self.assertIn("button.closest('.msg')", RUN_DETAILS_JS)
        self.assertIn("panel.scrollIntoView({ block: 'nearest' })", RUN_DETAILS_JS)
        self.assertIn("const cache = {};", RUN_DETAILS_JS)
        self.assertNotIn("run-details-drawer", UI_SOURCE)
        self.assertNotIn("data-act=\"run-details\"", HTML)
        self.assertNotIn("Last run details", HTML)
        self.assertNotIn("localStorage", RUN_DETAILS_JS)
        self.assertNotIn("persistActive", RUN_DETAILS_JS)
        for term in (
            "RunTrace",
            "PromptEnvelope",
            "Policy Pipeline",
            "Router",
            "Ghost",
            "Hebbian",
            "Directive",
            "Provider",
        ):
            self.assertNotIn(term, RUN_DETAILS_JS)

    def test_run_details_style_uses_existing_design_tokens(self) -> None:
        start = APP_CSS.index(".run-details {")
        end = APP_CSS.index("/* ---------- changes card ---------- */", start)
        block = APP_CSS[start:end]

        self.assertIn("border-top: 1px solid var(--border-2)", block)
        self.assertIn("color: var(--muted)", block)
        self.assertIn("color: var(--text-dim)", block)
        self.assertIn(".run-details-row.warning .run-details-value { color: var(--text-dim); }", block)
        self.assertIn("font-size: 10.5px", block)
        self.assertIn("font-size: 12px", block)
        self.assertIn("letter-spacing: 1px", block)
        self.assertIn("text-transform: uppercase", block)
        for forbidden in ("box-shadow", "background:", "border-radius", "#", "rgba(", "var(--err-text)"):
            self.assertNotIn(forbidden, block)

    def test_local_context_group_titles_follow_group_label_style(self) -> None:
        start = APP_CSS.index(".local-context-group-title")
        end = APP_CSS.index(".local-context-row", start)
        block = APP_CSS[start:end]
        self.assertIn("font-size: 10.5px", block)
        self.assertIn("text-transform: uppercase", block)
        self.assertIn("letter-spacing: 1px", block)
        self.assertIn("color: var(--muted)", block)

    def test_local_context_empty_state_is_single_summary(self) -> None:
        self.assertIn("No local context yet", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("No local context yet · Updates ${state}", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("const hasContent = !!(contextRows.length || reviewRows.length || activeRows.length || taskRows.length);", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("const hasWarning = !!warnings.length;", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("if (!hasContent && !hasWarning)", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("appendGroup(body, 'Recent focus', contextRows, rowNode);", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("appendGroup(body, 'Active preferences', activeRows, rowNode);", LOCAL_CONTEXT_DRAWER_JS)
        self.assertNotIn("empty.textContent = 'None';", LOCAL_CONTEXT_DRAWER_JS)

    def test_local_context_settings_are_visually_separated(self) -> None:
        start = APP_CSS.index(".local-context-settings")
        end = APP_CSS.index(".drawer-btn.arming", start)
        block = APP_CSS[start:end]
        self.assertIn("border-top: 1px solid var(--border-2)", block)

    def test_drawer_opening_is_mutually_exclusive(self) -> None:
        self.assertIn("function closeOtherDrawers(active)", HTML)
        self.assertIn("if (active !== 'changes') closeChangesDrawer();", HTML)
        self.assertIn("if (active !== 'research') closeResearchDrawer();", HTML)
        self.assertIn("if (active !== 'local_context') closeLocalContextDrawer();", HTML)
        self.assertIn("deps.closeOtherDrawers('changes')", CHANGES_DRAWER_JS)
        self.assertIn("deps.closeOtherDrawers('research')", RESEARCH_DRAWER_JS)
        self.assertIn("deps.closeOtherDrawers('local_context')", LOCAL_CONTEXT_DRAWER_JS)

    def test_local_context_drawer_binds_and_closes_on_scope_change(self) -> None:
        self.assertNotIn("lastSummary", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("let loadedScope = null;", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("const requestScope = currentScope();\n  loadedScope = requestScope;", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("loadedScope = data && data.scope ? cleanScope(data.scope) : requestScope;", LOCAL_CONTEXT_DRAWER_JS)
        self.assertGreaterEqual(
            LOCAL_CONTEXT_DRAWER_JS.count("if (!isOpen() || !sameScope(requestScope, currentScope())) return;"),
            2,
        )
        self.assertIn("function handleScopeChanged()", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("handleScopeChanged,", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("function normalizeProjectPath(value)", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn(r"replace(/\\/g, '/')", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("function handleLocalContextScopeChanged()", HTML)
        self.assertIn("persistActiveNow(); handleLocalContextScopeChanged(); renderSidebar(); renderChat();", HTML)
        self.assertIn("if (sessionId === activeId) handleLocalContextScopeChanged();", HTML)
        self.assertIn("if (!loadedScope || !sameScope(loadedScope, currentScope()))", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("session_id: scope.session_id", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("project: scope.project", LOCAL_CONTEXT_DRAWER_JS)
        export_start = LOCAL_CONTEXT_DRAWER_JS.index("async function exportLocalContext()")
        export_end = LOCAL_CONTEXT_DRAWER_JS.index("window.CodeyLocalContextDrawer", export_start)
        export_block = LOCAL_CONTEXT_DRAWER_JS[export_start:export_end]
        self.assertIn("if (!loadedScope || !sameScope(loadedScope, currentScope()))", export_block)

    def test_local_context_running_items_do_not_show_reject_action(self) -> None:
        self.assertIn("['candidate', 'queued', 'blocked'].includes(row.status)", LOCAL_CONTEXT_DRAWER_JS)
        self.assertNotIn("['candidate', 'queued', 'running', 'blocked'].includes(row.status)", LOCAL_CONTEXT_DRAWER_JS)

    def test_clear_messages_closes_local_context_drawer(self) -> None:
        start = HTML.index("async function clearMessages(id)")
        end = HTML.index("function renameProject", start)
        block = HTML[start:end]
        self.assertIn("if (!await forgetSessionState(id)) return;", block)
        self.assertIn("closeLocalContextDrawer();", block)

    def test_retry_uses_current_session_model_picker(self) -> None:
        retry_start = HTML.index("function retryTask(sessionId)")
        retry_end = HTML.index("function sessionProjectPath", retry_start)
        retry_block = HTML[retry_start:retry_end]
        self.assertIn("syncProviderUI(s.provider || DEFAULT_PROVIDER)", retry_block)
        self.assertIn("$('send').click()", retry_block)
        self.assertIn("await sendTaskFromSession(sessionId, task, provider, () => clearDraftIfUnchanged(sessionId, task));", COMPOSER_JS)

    def test_provider_selector_is_enabled_when_idle(self) -> None:
        self.assertIn("$('provider-button').disabled = busy", HTML)
        self.assertIn("$('provider-button').disabled = false", HTML)
        self.assertNotIn("btn.disabled = !providerStatus", HTML)

    def test_sse_reconnect_reconciles_one_run_snapshot_quietly(self) -> None:
        self.assertIn("function reconcileRunState()", HTML)
        self.assertIn("return window.CodeySse.reconcileRunState();", HTML)
        self.assertIn("fetch('/api/state', { cache: 'no-store' })", SSE_JS)
        self.assertIn("runningRunId", HTML)
        self.assertIn("data.pending_event", HTML)
        self.assertIn("data.last_terminal_event", HTML)
        self.assertIn("data.last_shell_result", HTML)
        self.assertIn("markTerminalRun", HTML)
        self.assertIn("if (reconcilePromise) return reconcilePromise", SSE_JS)
        self.assertIn("bufferedServerEvents.push(data)", SSE_JS)
        self.assertIn("for (const event of events) deps.handleServerEvent(event)", SSE_JS)
        self.assertIn("ingestServerEvent(data)", SSE_JS)
        self.assertIn("}, 5000);", SSE_JS)
        self.assertIn("deps.setStatus('Reconnecting...', 'warn')", SSE_JS)
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
        self.assertIn("if (menu.classList.contains('open')) refreshProviderStatus();", PROVIDER_UI_JS)
        self.assertIn("if (data.type === 'providers')", HTML)
        self.assertIn("applyProviderStatus(data.providers)", HTML)
        self.assertIn("data.provider_failure.action === 'connect'", HTML)
        self.assertIn("applyProviderStatus([{ id: data.provider, available: false }])", HTML)
        self.assertIn("addSendError(sid, terminalKey, runId)", HTML)
        self.assertNotIn("Could not connect model", UI_SOURCE)
        self.assertNotIn("Open the model page", UI_SOURCE)
        self.assertNotIn("PlaywrightContextManager", UI_SOURCE)

    def test_deleting_last_session_preserves_selected_provider(self) -> None:
        self.assertIn("const fallbackProvider = currentProviderId()", HTML)
        self.assertIn("defaultSession(null, fallbackProvider)", HTML)

    def test_topbar_shows_running_spinner(self) -> None:
        self.assertIn(".spinner", STYLE_SOURCE)
        self.assertIn('id="status"', HTML)
        self.assertIn("setStatus('Running', 'run')", HTML)
        self.assertIn("drawer-loading", STYLE_SOURCE)
        self.assertIn("class=\"spinner\"", CHANGES_DRAWER_JS)
        self.assertIn("spinner.className = 'spinner';", RESEARCH_DRAWER_JS)
        self.assertIn("spinner.className = 'spinner';", RUN_DETAILS_JS)
        self.assertIn("drawer-loading", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("class=\"spinner\"", LOCAL_CONTEXT_DRAWER_JS)

    def test_status_rows_use_continue_and_retry_links(self) -> None:
        self.assertIn(".link-btn", STYLE_SOURCE)
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
        # Receipts are structured (0.5 schema): the UI reads them through
        # the shared render.js helpers, never flat text fields.
        self.assertIn("receiptSummary(data.receipt)", HTML)
        self.assertIn("text: receiptSummary(data.receipt)", HTML)
        self.assertIn("const receiptSummary = window.CodeyRender.receiptSummary;", HTML)
        self.assertIn("const receiptChangedCount = window.CodeyRender.receiptChangedCount;", HTML)
        self.assertIn("label: 'View diff'", HTML)
        self.assertIn("onclick: () => openChangesDrawer(m.project)", HTML)
        self.assertIn("let shownReceipt = false", HTML)
        self.assertIn("shownReceipt = true", HTML)
        self.assertIn("data.changed && !shownReceipt", HTML)
        self.assertIn("type: 'changes'", HTML)

    def test_research_context_is_explicit_and_user_facing(self) -> None:
        self.assertIn('id="ctx-research"', HTML)
        self.assertIn('>Research</button>', HTML)
        self.assertIn('class="ctx-token ctx-folder"', HTML)
        self.assertIn('id="provider-button"', HTML)
        self.assertIn('id="provider-name"', HTML)
        self.assertNotIn('id="ctx-provider"', HTML)
        self.assertIn('id="research-drawer"', HTML)
        self.assertIn('<strong>Research</strong>', HTML)
        self.assertIn("function currentIntentForSession(sessionId)", HTML)
        self.assertIn("return sessionProjectPath(sessionId) ? 'hybrid' : 'research';", HTML)
        self.assertIn("body: JSON.stringify({ session_id: sessionId, project, task: text, provider, intent })", COMPOSER_JS)
        self.assertIn("type: 'research_done'", HTML)
        self.assertIn("Research restored:", HTML)
        self.assertIn("$('research-restore').disabled = !current || !current.restoreable;", HTML)
        self.assertIn("markResearchRestoreAvailability(data.research_restore_runs || [])", HTML)
        self.assertIn("function normalizeStoredResearchRun(run)", UI_STATE_JS)
        self.assertIn("s.researchRuns.slice(-32).map(normalizeStoredResearchRun)", UI_STATE_JS)
        self.assertNotIn("...run, restoreable: false", HTML)
        self.assertIn("function normalizeResearchRunArtifact(runId, research, receipt)", HTML)
        self.assertIn("research-tabs", RESEARCH_DRAWER_JS)
        self.assertIn("research-tab", RESEARCH_DRAWER_JS)
        self.assertIn("Evidence", RESEARCH_DRAWER_JS)
        self.assertIn("Sources", RESEARCH_DRAWER_JS)
        self.assertIn("Graph", RESEARCH_DRAWER_JS)
        self.assertIn("Notes", RESEARCH_DRAWER_JS)
        self.assertIn("citationMap", HTML)
        self.assertIn("evidenceItems", HTML)
        self.assertIn("openedSources", HTML)
        self.assertIn("qualityWarnings", HTML)
        self.assertIn("renderResearchEvidence", RESEARCH_DRAWER_JS)
        self.assertIn("renderResearchSources", RESEARCH_DRAWER_JS)
        self.assertIn("const locator = item.locator || (item.page ? `p.${item.page}` : '');", RESEARCH_DRAWER_JS)
        self.assertIn("formatPdfSourceMeta", RESEARCH_DRAWER_JS)
        self.assertIn("compactPages", RESEARCH_DRAWER_JS)
        self.assertIn("PDF", RESEARCH_DRAWER_JS)
        self.assertIn("pages ${pages} / ${pageCount}", RESEARCH_DRAWER_JS)
        self.assertIn("panel.appendChild(researchSourceCard({\n      ...source,", RESEARCH_DRAWER_JS)
        self.assertNotIn("['coverage', 'Coverage']", UI_SOURCE)
        self.assertIn("Search coverage", RESEARCH_DRAWER_JS)
        self.assertIn("appendResearchCoverage", RESEARCH_DRAWER_JS)
        self.assertIn("['graph', 'Graph']", RESEARCH_DRAWER_JS)
        self.assertNotIn("['concepts'", RESEARCH_DRAWER_JS)
        self.assertIn("renderResearchGraph(panel, run, sessionId)", RESEARCH_DRAWER_JS)
        self.assertNotIn("renderResearchConcepts", RESEARCH_DRAWER_JS)
        self.assertNotIn("endpoint: '/api/research/concept_graph'", RESEARCH_DRAWER_JS)
        self.assertNotIn("showDepth: false", RESEARCH_DRAWER_JS)
        self.assertIn('<script src="/assets/research_graph.js?v=__CODEY_VERSION__"></script>', HTML)
        self.assertIn('<script src="/assets/research_drawer.js?v=__CODEY_VERSION__"></script>', HTML)
        self.assertIn("window.CodeyResearchDrawer.init({", HTML)
        self.assertIn("window.CodeyResearchDrawer = {", RESEARCH_DRAWER_JS)
        self.assertIn("if (window.CodeyResearchGraph) window.CodeyResearchGraph.dispose();", RESEARCH_DRAWER_JS)
        self.assertIn("window.CodeyResearchGraph.render(panel, {", RESEARCH_DRAWER_JS)
        self.assertIn("focusIds: run.synthesisId ? [run.synthesisId] : coreNoteIdsForResearchRun(run)", RESEARCH_DRAWER_JS)
        self.assertIn("onOpenNote(noteId)", RESEARCH_DRAWER_JS)
        self.assertIn("onOpenSource(url)", RESEARCH_DRAWER_JS)
        self.assertIn("window.CodeyResearchGraph = { render, dispose };", GRAPH_JS)
        self.assertIn("async function loadGraph(options, canvas, status, detail)", GRAPH_JS)
        self.assertIn("function draw(canvas, graph, detail, options)", GRAPH_JS)
        self.assertIn("function setDetailBody(body, text)", GRAPH_JS)
        self.assertIn("window.CodeyRender.renderMarkdown(body, value);", GRAPH_JS)
        self.assertIn("fetch((options.endpoint || '/api/research/graph') + '?' + params.toString())", GRAPH_JS)
        self.assertIn("if (options.showDepth !== false) {", GRAPH_JS)
        self.assertIn("for (const depth of [1, 2, 3])", GRAPH_JS)
        self.assertIn("function graphLayerY(node)", GRAPH_JS)
        self.assertIn("const layerY = graphLayerY(node);", GRAPH_JS)
        self.assertIn("const warning = Array.isArray(graph.warnings) && graph.warnings.length ? graph.warnings[0] : '';", GRAPH_JS)
        self.assertIn("setStatus(status, warning || 'No graph yet');", GRAPH_JS)
        self.assertIn("node.kind === 'concept'", GRAPH_JS)
        self.assertIn("edge.kind === 'cites' || edge.kind === 'tagged'", GRAPH_JS)
        self.assertIn("let loadSeq = 0;", GRAPH_JS)
        self.assertIn("loadSeq += 1;", GRAPH_JS)
        self.assertIn("const seq = ++loadSeq;", GRAPH_JS)
        self.assertIn("seq !== loadSeq || !canvas.isConnected", GRAPH_JS)
        self.assertIn("const nextRuntime = draw(canvas, graph, detail, options);", GRAPH_JS)
        self.assertIn("if (nextRuntime && typeof nextRuntime.stop === 'function') nextRuntime.stop();", GRAPH_JS)
        self.assertNotIn("researchGraphRuntime", UI_SOURCE)
        self.assertNotIn("researchGraphLoadSeq", UI_SOURCE)
        self.assertNotIn("function loadResearchGraph", UI_SOURCE)
        self.assertNotIn("function drawResearchGraph", UI_SOURCE)
        self.assertIn(".research-graph-status.error { color: var(--err-text); }", STYLE_SOURCE)
        self.assertIn("function setStatus(status, text, isError = false)", GRAPH_JS)
        self.assertIn("setStatus(status, 'Loading graph...');", GRAPH_JS)
        self.assertNotIn("setStatus(status, 'No graph yet');", GRAPH_JS)
        self.assertIn("setStatus(status, data.error || 'Graph unavailable', true);", GRAPH_JS)
        self.assertIn("setStatus(status, 'Graph unavailable', true);", GRAPH_JS)
        self.assertIn("status.classList.toggle('error', !!isError);", GRAPH_JS)
        self.assertIn("status.classList.remove('error');", GRAPH_JS)
        self.assertIn("status.hidden = false;", GRAPH_JS)
        self.assertIn("status.textContent = text;", GRAPH_JS)
        self.assertIn("research-graph-stage", UI_SOURCE)
        self.assertIn("research-depth", UI_SOURCE)
        self.assertIn("Reset", GRAPH_JS)
        self.assertIn("ctx.setLineDash([5, 5])", GRAPH_JS)
        self.assertIn("ctx.strokeStyle = hoverActive ? colors.ok", GRAPH_JS)
        self.assertIn("ctx.fillStyle = hovered ? colors.ok", GRAPH_JS)
        self.assertIn("action.onclick = () => openNode(node, options);", GRAPH_JS)
        self.assertIn("function onDblClick(event)", GRAPH_JS)
        self.assertIn("if (hit) openNode(hit, options);", GRAPH_JS)
        self.assertIn("setDetail(detail, hit, options);", GRAPH_JS)
        self.assertNotIn("if (hit && hit.url) openNode(hit, options);", GRAPH_JS)
        self.assertNotIn("if (hit && hit.url) openResearchGraphNode(hit, run, sessionId);", UI_SOURCE)
        self.assertNotIn("node.focus ? colors.ok", GRAPH_JS)
        self.assertNotIn("graph: data.graph", UI_SOURCE)
        self.assertIn("loadResearchRunNotes(run, sessionId)", RESEARCH_DRAWER_JS)
        self.assertIn("Note no longer exists", RESEARCH_DRAWER_JS)
        self.assertIn("__state: 'missing'", RESEARCH_DRAWER_JS)
        self.assertIn("invalidateResearchNoteCache(noteIdsForResearchRun(run))", HTML)
        self.assertIn("function renderResearchNotes(panel, run, sessionId)", RESEARCH_DRAWER_JS)
        self.assertIn("const NOTE_PREVIEW_CHARS = 1500;", RESEARCH_DRAWER_JS)
        self.assertIn("const expandedResearchNoteIds = new Set();", RESEARCH_DRAWER_JS)
        self.assertIn("panel.appendChild(researchEmpty('No notes recorded'))", RESEARCH_DRAWER_JS)
        self.assertIn("['Selected note', selectedIds]", RESEARCH_DRAWER_JS)
        self.assertIn("['Synthesis', synthesisIds]", RESEARCH_DRAWER_JS)
        self.assertIn("['Created notes', createdIds]", RESEARCH_DRAWER_JS)
        self.assertIn("['Updated notes', updatedIds]", RESEARCH_DRAWER_JS)
        self.assertNotIn("panel.appendChild(researchSection('Sources'", RESEARCH_DRAWER_JS)
        self.assertIn("research-note-section", RESEARCH_DRAWER_JS)
        self.assertIn("research-card research-note-card", RESEARCH_DRAWER_JS)
        self.assertIn("research-note-body md", RESEARCH_DRAWER_JS)
        self.assertIn("window.CodeyRender.renderMarkdown(body, preview || 'No note body');", RESEARCH_DRAWER_JS)
        self.assertIn("Show more", RESEARCH_DRAWER_JS)
        self.assertIn("Show less", RESEARCH_DRAWER_JS)
        self.assertIn("function buildResearchSourceIndex(run)", RESEARCH_DRAWER_JS)
        self.assertIn("item.url || item.final_url || item.requested_url", RESEARCH_DRAWER_JS)
        self.assertIn("put(item.requested_url, item, meta)", RESEARCH_DRAWER_JS)
        self.assertIn("item.number || existing.number || (aliasOf && aliasOf.number)", RESEARCH_DRAWER_JS)
        self.assertIn("item.title || existing.title || (aliasOf && aliasOf.title)", RESEARCH_DRAWER_JS)
        self.assertIn("function safeResearchUrl(value)", RESEARCH_DRAWER_JS)
        self.assertIn("url.protocol === 'http:' || url.protocol === 'https:'", RESEARCH_DRAWER_JS)
        self.assertIn("chip.className = 'research-source-chip';", RESEARCH_DRAWER_JS)
        self.assertIn("window.open(ref.url, '_blank', 'noopener,noreferrer');", RESEARCH_DRAWER_JS)
        self.assertNotIn("innerHTML = note.body", RESEARCH_DRAWER_JS)
        self.assertNotIn("research-note-text", RESEARCH_DRAWER_JS)
        self.assertNotIn(".research-note-text", APP_CSS)
        self.assertIn(".research-note-body", APP_CSS)
        self.assertIn(".research-source-chip", APP_CSS)
        self.assertNotIn("pre.className = 'diff-pre';", RESEARCH_DRAWER_JS)
        self.assertIn("Use in Project", HTML)
        self.assertNotIn("Use vault", HTML)
        self.assertNotIn("Knowledge mode", HTML)

        ctx_css = APP_CSS[APP_CSS.index(".ctx-token {"):APP_CSS.index(".composer-context .ctx-sep")]
        self.assertIn("border: 0;", ctx_css)
        self.assertIn(".ctx-token:hover:not(:disabled) { color: var(--text); }", ctx_css)
        self.assertNotIn("font-weight", ctx_css)
        self.assertNotIn("background: var(--hover)", ctx_css)
        self.assertNotIn("border: 1px", ctx_css)

    def test_agent_events_are_structured_instead_of_parsed_from_logs(self) -> None:
        self.assertIn("if (data.type === 'turn')", HTML)
        self.assertIn("if (data.type === 'tool')", HTML)
        self.assertIn("if (data.type === 'info')", HTML)
        turn_start = HTML.index("if (data.type === 'turn')")
        turn_end = HTML.index("if (data.type === 'tool_started')", turn_start)
        turn_block = HTML[turn_start:turn_end]
        self.assertIn("note: (data.note || '').toString()", turn_block)
        render_start = HTML.index("} else if (m.type === 'turn') {")
        render_end = HTML.index("} else if (m.type === 'asst') {", render_start)
        render_block = HTML[render_start:render_end]
        self.assertIn("const note = (m.note || '').toString().trim();", render_block)
        self.assertIn("label.textContent = `Turn ${m.n}${note ? ` ${note}` : ''}`;", render_block)
        self.assertNotIn("const turnMatch = line.match", HTML)
        self.assertNotIn("const toolMatch = line.match", HTML)
        self.assertNotIn("if (data.type === 'log')", HTML)
        self.assertNotIn("function handleLog", HTML)

    def test_tool_start_pending_row_is_replaced_by_final_tool(self) -> None:
        self.assertIn(".tool-line.pending .tl-result", STYLE_SOURCE)
        self.assertIn("if (m.pending) row.classList.add('pending');", RENDER_JS)
        self.assertIn(
            "result.textContent = m.pending ? (m.activity || m.result || 'Working') : (m.result || '');",
            RENDER_JS,
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
        self.assertIn("const toolStatus = (data.status || (data.ok === false ? 'error' : 'ok')).toString();", final_block)
        self.assertIn("status: toolStatus,", final_block)
        self.assertIn("error: toolStatus === 'error',", final_block)
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
        self.assertIn("if (r.status === 409) { deps.addSendError(sessionId); return true; }", COMPOSER_JS)
        self.assertIn("if (r.status === 409 || !r.ok) {", COMPOSER_JS)
        self.assertIn("await deps.acceptRunResponse(r, sessionId)", COMPOSER_JS)
        self.assertIn("actions: window.CodeyRunDetails.actionsForMessage(m, [", HTML)
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
        self.assertIn("e.key === 'Enter' && !e.shiftKey", COMPOSER_JS)
        self.assertIn("!e.isComposing", COMPOSER_JS)
        self.assertIn("e.keyCode !== 229", COMPOSER_JS)
        self.assertNotIn("e.key === 'Enter' && (e.ctrlKey || e.metaKey)", UI_SOURCE)
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
        self.assertIn("{ type: 'review', text: data.text, sessionId: sid, runId }", HTML)
        self.assertIn("statusRow('Review'", HTML)
        review_start = HTML.index("} else if (m.type === 'review') {")
        review_end = HTML.index("} else if (m.type === 'changes') {", review_start)
        review_block = HTML[review_start:review_end]
        self.assertNotIn("CodeyRunDetails", review_block)
        self.assertNotIn("Review mode", HTML)

    def test_plain_chat_terminal_event_can_restore_assistant_reply(self) -> None:
        done_start = HTML.index("if (data.type === 'task_done')")
        done_end = HTML.index("if (data.type === 'shell_request')", done_start)
        done_block = HTML[done_start:done_end]
        self.assertIn("data.mode === 'chat'", done_block)
        self.assertIn("type: 'asst', text: summary, runId, eventKey: answerKey", done_block)
        self.assertIn("data.mode !== 'research' && data.mode !== 'chat'", done_block)

        reply_start = HTML.index("if (data.type === 'reply')")
        reply_end = HTML.index("if (data.type === 'review')", reply_start)
        reply_block = HTML[reply_start:reply_end]
        self.assertIn("const answerKey = runId ? `terminal:${runId}:answer` : '';", reply_block)
        self.assertIn("type: 'asst', text: data.text, runId, eventKey: answerKey", reply_block)

    def test_stopped_terminal_event_renders_status_row(self) -> None:
        done_start = HTML.index("if (data.type === 'task_done')")
        done_end = HTML.index("if (data.type === 'shell_request')", done_start)
        done_block = HTML[done_start:done_end]

        self.assertIn("reason === 'stopped'", done_block)
        self.assertIn("Stopped after ${turns} turn", done_block)
        self.assertIn("type: 'pause', text: label", done_block)

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
        self.assertIn("Reading changes", CHANGES_DRAWER_JS)
        self.assertIn("data.mode === 'git' ? 'Git' : 'Snapshot'", CHANGES_DRAWER_JS)
        self.assertIn("$('changes-restore').hidden = data.mode === 'git';", CHANGES_DRAWER_JS)
        self.assertIn("/api/changes/restore", HTML)
        self.assertNotIn("Reading git diff", UI_SOURCE)
        self.assertIn('<script src="/assets/changes_drawer.js?v=__CODEY_VERSION__"></script>', HTML)
        self.assertIn("window.CodeyChangesDrawer.init({", HTML)
        self.assertIn("window.CodeyChangesDrawer = {", CHANGES_DRAWER_JS)

    def test_changes_drawer_hides_diff_metadata_lines(self) -> None:
        self.assertIn('class="diff-line add"><span class="ln"', CHANGES_DRAWER_JS)
        self.assertIn('class="diff-line del"><span class="ln"', CHANGES_DRAWER_JS)
        self.assertIn("line.startsWith('diff --git')", CHANGES_DRAWER_JS)
        self.assertNotIn("line.startsWith('@@')) cls += ' hunk'", UI_SOURCE)

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
        update_start = COMPOSER_JS.index("function updateSend()")
        update_end = COMPOSER_JS.index("function toggleResearchForActive()", update_start)
        update_block = COMPOSER_JS[update_start:update_end]
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
        self.assertIn("function addMessageCopyButton(div, text)", RENDER_JS)
        self.assertIn("function copyText(text)", RENDER_JS)
        self.assertIn("navigator.clipboard.writeText(value)", RENDER_JS)
        self.assertIn("document.execCommand('copy')", RENDER_JS)
        self.assertIn("className = 'msg-copy'", RENDER_JS)
        self.assertIn("opacity: .45", STYLE_SOURCE)
        self.assertIn(".msg:hover .msg-copy", STYLE_SOURCE)
        self.assertIn(".msg:focus-within .msg-copy", STYLE_SOURCE)
        self.assertIn("aria-label', 'Copy message'", RENDER_JS)
        self.assertIn("addMessageCopyButton(div, messageCopyText(m))", HTML)
        self.assertIn("copyText: text", HTML)
        self.assertIn('<script src="/assets/render.js?v=__CODEY_VERSION__"></script>', HTML)
        self.assertIn("window.CodeyRender = {", RENDER_JS)
        self.assertIn("const copyText = window.CodeyRender.copyText;", HTML)
        self.assertNotIn("Export chat", UI_SOURCE)

    def test_chat_messages_and_titles_remain_local_persistent(self) -> None:
        self.assertIn("const LS_SESSIONS = 'codey:sessions';", UI_STATE_JS)
        self.assertIn("messages: Array.isArray(s.messages) ? s.messages : []", UI_STATE_JS)
        self.assertIn("title: s.title || 'New chat'", UI_STATE_JS)
        self.assertIn("function safeLocalSet(key, value)", UI_STATE_JS)
        self.assertIn("function saveSessions(arr)", UI_STATE_JS)
        self.assertIn("safeLocalSet(LS_SESSIONS, JSON.stringify(arr))", UI_STATE_JS)
        self.assertIn("try { localStorage.setItem(key, value); } catch {}", UI_STATE_JS)
        self.assertNotIn("LS_LEGACY_PROJECT", HTML)
        self.assertIn("s.title = m.text.slice(0, 28)", HTML)
        self.assertIn("s.title = title.slice(0, 80)", HTML)

    def test_chat_state_uses_backend_snapshot_with_local_cache(self) -> None:
        self.assertIn('<script src="/assets/ui_state.js?v=__CODEY_VERSION__"></script>', HTML)
        self.assertIn("function currentUiState()", UI_STATE_JS)
        self.assertIn("function cacheUiState()", UI_STATE_JS)
        self.assertIn("function saveUiStateToServer()", UI_STATE_JS)
        self.assertIn("async function restoreUiStateFromServer()", UI_STATE_JS)
        self.assertIn("function bindUiStatePagehide()", UI_STATE_JS)
        self.assertIn("const LS_UI_REVISION = 'codey:ui-revision';", UI_STATE_JS)
        self.assertIn("revision: uiStateRevision", UI_STATE_JS)
        self.assertIn("function isUiStateNewer(a, b)", UI_STATE_JS)
        self.assertIn("uiStateRevision += 1", UI_STATE_JS)
        self.assertIn("function hasMeaningfulUiState(state)", UI_STATE_JS)
        self.assertIn("const bootHadMeaningfulUiState = hasMeaningfulUiState(bootCachedState)", HTML)
        self.assertIn("fetch('/api/ui_state'", UI_STATE_JS)
        self.assertIn("method: 'POST'", UI_STATE_JS)
        self.assertIn("body: JSON.stringify({ state: currentUiState() })", UI_STATE_JS)
        self.assertIn("navigator.sendBeacon('/api/ui_state'", UI_STATE_JS)
        self.assertIn("function connectEvents()", HTML)
        self.assertIn("bindUiStatePagehide();", HTML)
        self.assertIn("await restoreUiStateFromServer();", HTML)
        boot_start = HTML.index("async function boot()")
        boot_block = HTML[boot_start:HTML.index("boot();", boot_start)]
        self.assertIn("refreshProviderStatus();", boot_block)
        self.assertIn("connectEvents();", HTML)
        self.assertLess(HTML.index("await restoreUiStateFromServer();"), HTML.index("connectEvents();"))
        self.assertIn("persistActive();", HTML[HTML.index("function ensureProject"):HTML.index("async function pickProject")])
        self.assertNotIn("const LS_SESSIONS = 'codey:sessions';", HTML)
        self.assertNotIn("function normalizeStoredResearchRun(run)", HTML)

    def test_assistant_replies_render_minimal_markdown(self) -> None:
        self.assertIn("function renderMarkdown(container, text)", RENDER_JS)
        self.assertIn("function renderInlineMd(text)", RENDER_JS)
        self.assertIn("const escaped = escapeHtml(text);", RENDER_JS)
        self.assertIn("<strong>$1</strong>", RENDER_JS)
        self.assertIn("body.className = 'body md'", HTML)
        self.assertNotIn("body.className = 'body md collapsed'", HTML)
        self.assertIn("toggle.className = 'toggle'; toggle.textContent = 'Collapse';", HTML)
        self.assertIn("renderMarkdown(body, m.text)", HTML)
        self.assertNotIn("body.textContent = m.text;", UI_SOURCE)
        self.assertIn("/^#{1,6}\\s+/.test(line)", RENDER_JS)
        self.assertIn("line.match(/^(#{1,6})\\s+(.*)$/)", RENDER_JS)
        self.assertIn("el.className = 'md-h md-h' + Math.min(6, heading[1].length);", RENDER_JS)
        self.assertIn(".md-code", STYLE_SOURCE)
        self.assertIn(".md-ic", STYLE_SOURCE)
        self.assertIn(".md-list", STYLE_SOURCE)
        self.assertIn(".md-list .md-list", STYLE_SOURCE)
        self.assertIn("const escaped = escapeHtml(text);", RENDER_JS)
        self.assertIn("el.className = 'md-quote';", RENDER_JS)
        self.assertIn(".md-quote", STYLE_SOURCE)
        self.assertIn("let listStack = [];", RENDER_JS)
        self.assertIn("parent.lastLi.appendChild(next);", RENDER_JS)
        self.assertIn("background: var(--panel-2)", STYLE_SOURCE)

    def test_inline_code_spans_are_not_bolded(self) -> None:
        self.assertIn("function applyBold(segment)", RENDER_JS)
        self.assertIn("out += applyBold(escaped.slice(last, m.index));", RENDER_JS)
        self.assertIn("${m[1]}</code>", RENDER_JS)
        self.assertIn("out += applyBold(escaped.slice(last));", RENDER_JS)

    def test_markdown_stays_monochrome_without_syntax_highlighting(self) -> None:
        md_css_start = APP_CSS.index("assistant markdown")
        md_css_end = APP_CSS.index("tool line", md_css_start)
        md_css = APP_CSS[md_css_start:md_css_end]
        self.assertNotIn("#", md_css.replace("var(--", ""))
        self.assertNotIn("hljs", UI_SOURCE)
        self.assertNotIn("highlight.js", UI_SOURCE)

    def test_code_blocks_have_quiet_copy_button(self) -> None:
        self.assertIn("function addCodeCopyButton(pre, text)", RENDER_JS)
        self.assertIn("function appendCodeBlock(container, code)", RENDER_JS)
        self.assertIn("className = 'code-copy'", RENDER_JS)
        self.assertIn("aria-label', 'Copy code'", RENDER_JS)
        self.assertIn("el.textContent = code", RENDER_JS)
        self.assertIn(".md-code:hover .code-copy", STYLE_SOURCE)
        self.assertIn("await copyText(value)", RENDER_JS)

    def test_ui_state_persistence_is_debounced_with_immediate_flush_helpers(self) -> None:
        # A-1: hot-path persistence is coalesced behind a debounce timer, while
        # discrete/terminal moments flush immediately. Data shape is unchanged.
        self.assertIn("function markUiStateDirty()", UI_STATE_JS)
        self.assertIn("function flushUiState()", UI_STATE_JS)
        self.assertIn("function persistActive()", UI_STATE_JS)
        self.assertIn("function persistActiveNow()", UI_STATE_JS)
        self.assertIn("let uiStatePersistTimer = null;", UI_STATE_JS)
        self.assertIn("const UI_STATE_PERSIST_DELAY = 400;", UI_STATE_JS)
        # revision bump preserved (relied on by isUiStateNewer ordering).
        self.assertIn("uiStateRevision += 1", UI_STATE_JS)

        dirty_start = UI_STATE_JS.index("function markUiStateDirty()")
        dirty_end = UI_STATE_JS.index("function flushUiState()", dirty_start)
        dirty_block = UI_STATE_JS[dirty_start:dirty_end]
        self.assertIn("uiStateUpdatedAt = Math.max(Date.now(), uiStateUpdatedAt);", dirty_block)
        self.assertIn("uiStateRevision += 1;", dirty_block)
        self.assertIn("uiStateDirtySinceBoot = true;", dirty_block)

        flush_start = UI_STATE_JS.index("function flushUiState()")
        flush_end = UI_STATE_JS.index("function persistActive()", flush_start)
        flush_block = UI_STATE_JS[flush_start:flush_end]
        self.assertIn("clearTimeout(uiStatePersistTimer)", flush_block)
        self.assertIn("cacheUiState();", flush_block)
        self.assertIn("saveUiStateToServer();", flush_block)

        persist_start = UI_STATE_JS.index("function persistActive()")
        persist_end = UI_STATE_JS.index("function persistActiveNow()", persist_start)
        persist_block = UI_STATE_JS[persist_start:persist_end]
        self.assertIn("markUiStateDirty();", persist_block)
        self.assertIn("if (uiStatePersistTimer !== null) return;", persist_block)
        self.assertIn("uiStatePersistTimer = setTimeout(", persist_block)
        self.assertIn("UI_STATE_PERSIST_DELAY", persist_block)

        now_start = UI_STATE_JS.index("function persistActiveNow()")
        now_end = UI_STATE_JS.index("function bindUiStatePagehide()", now_start)
        now_block = UI_STATE_JS[now_start:now_end]
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
        self.assertNotIn("function pushMsg(m)", HTML)
        self.assertNotIn("pushMsg(", HTML)
        push_start = HTML.index("function pushMsgToSession(sid, m)")
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

        prov_start = COMPOSER_JS.index("function setActiveProvider(id)")
        prov_end = COMPOSER_JS.index("function clearDraftIfUnchanged", prov_start)
        self.assertIn("persistActiveNow();", COMPOSER_JS[prov_start:prov_end])

        # The debounced path is still used where coalescing matters: the SSE hot
        # path (addToSession) and low-signal toggles like ensureProject / expand.
        self.assertIn(
            "persistActive();",
            HTML[HTML.index("function ensureProject"):HTML.index("async function pickProject")],
        )

    def test_start_coding_from_this_chat_attaches_without_new_chat(self) -> None:
        self.assertIn("function sessionProject(s)", HTML)
        self.assertIn("return sessionProject(activeSession());", HTML)
        self.assertIn("const loose = sessions.filter(s => !sessionProject(s));", HTML)
        self.assertIn("const p = sessionProject(s);", HTML[HTML.index("function sessionProjectPath"):HTML.index("async function fetchChanges")])
        self.assertIn("const p = deps.sessionProject(s);", COMPOSER_JS[COMPOSER_JS.index("async function continueTask"):COMPOSER_JS.index("function bindHandlers")])
        self.assertIn("function attachSessionToProject(projectId, sessionId = activeId)", HTML)
        attach_start = HTML.index("function attachSessionToProject")
        attach_end = HTML.index("async function pickProject", attach_start)
        attach_block = HTML[attach_start:attach_end]
        self.assertIn("if (!s || !p || sessionProject(s)) return false;", attach_block)
        self.assertIn("s.projectId = p.id;", attach_block)
        self.assertIn("s.research = false;", attach_block)
        self.assertIn("persistActiveNow();", attach_block)
        self.assertNotIn("/api/new_chat", attach_block)

        pick_start = HTML.index("async function pickProject()")
        pick_end = HTML.index("async function attachCurrentChatToPickedProject", pick_start)
        pick_block = HTML[pick_start:pick_end]
        self.assertIn("const shouldAttach = !!s && !sessionProject(s);", pick_block)
        self.assertIn("pickProjectPath(shouldAttach ? 'new' : 'open'", pick_block)
        self.assertIn("if (shouldAttach) attachSessionToProject(p.id, s.id);", pick_block)
        self.assertIn("else newSession(p.id);", pick_block)

    def test_composer_context_is_the_only_draft_to_project_send_trigger(self) -> None:
        self.assertNotIn("'Choose folder to send'", HTML)
        self.assertIn("const proj = p ? p.name : 'Choose folder';", HTML)
        self.assertIn("'Choose folder'", HTML)
        self.assertIn('id="ctx-folder"', HTML)
        self.assertIn('id="ctx-research"', HTML)
        self.assertNotIn('id="ctx-provider"', HTML)
        self.assertNotIn("function providerLabel(id)", HTML)
        self.assertNotIn("ctx-provider", PROVIDER_UI_JS)
        self.assertIn('id="provider-button"', HTML)
        self.assertIn("$('task').addEventListener('input', () => { resizeTask(); updateSend(); deps.updateComposerContext(); });", COMPOSER_JS)

        context_start = COMPOSER_JS.index("$('composer-context').onclick")
        context_end = COMPOSER_JS.index("$('composer-context').addEventListener", context_start)
        context_block = COMPOSER_JS[context_start:context_end]
        self.assertIn("const target = e.target.closest('.ctx-token');", context_block)
        self.assertIn("if (target.id === 'ctx-folder')", context_block)
        self.assertIn("deps.attachCurrentChatToPickedProject({ sendDraft: !!$('task').value.trim() });", context_block)
        self.assertIn("toggleResearchForActive();", context_block)
        self.assertNotIn("ctx-provider", context_block)
        self.assertNotIn("openLocalProviderConfig();", context_block)
        key_context_start = COMPOSER_JS.index("$('composer-context').addEventListener", context_start)
        key_context_end = COMPOSER_JS.index("$('send').onclick", key_context_start)
        key_context_block = COMPOSER_JS[key_context_start:key_context_end]
        self.assertIn("e.key !== 'Enter' && e.key !== ' '", key_context_block)
        self.assertIn("target.click();", key_context_block)

        key_start = COMPOSER_JS.index("$('task').addEventListener('keydown'")
        key_end = COMPOSER_JS.index("$('composer-context').onclick", key_start)
        key_block = COMPOSER_JS[key_start:key_end]
        self.assertIn("$('send').click()", key_block)
        self.assertNotIn("pickProjectPath", key_block)
        self.assertNotIn("attachCurrentChatToPickedProject", key_block)

        send_start = COMPOSER_JS.index("async function sendActiveDraft()")
        send_end = COMPOSER_JS.index("async function continueTask", send_start)
        send_block = COMPOSER_JS[send_start:send_end]
        self.assertIn("const sessionId = activeId();", send_block)
        self.assertIn("const provider = currentProviderId();", send_block)
        self.assertIn("await sendTaskFromSession(sessionId, task, provider, () => clearDraftIfUnchanged(sessionId, task));", send_block)
        self.assertNotIn("pickProjectPath", send_block)
        self.assertNotIn("attachCurrentChatToPickedProject", send_block)

    def test_draft_to_project_send_uses_stable_session_id(self) -> None:
        self.assertIn("async function sendTaskFromSession(sessionId, task, providerId = '', onSendStarted = null)", COMPOSER_JS)
        self.assertIn("function clearDraftIfUnchanged(sessionId, draft)", COMPOSER_JS)
        send_start = COMPOSER_JS.index("async function sendTaskFromSession")
        send_end = COMPOSER_JS.index("async function sendActiveDraft", send_start)
        send_block = COMPOSER_JS[send_start:send_end]
        self.assertIn("if (!text || runningSessionId()) return false;", send_block)
        self.assertIn("if (!s) return false;", send_block)
        self.assertIn("if (typeof onSendStarted === 'function') onSendStarted();", send_block)
        self.assertIn("deps.pushMsgToSession(sessionId, { type: 'user', text });", send_block)
        self.assertIn("const project = deps.sessionProjectPath(sessionId);", send_block)
        self.assertIn("JSON.stringify({ session_id: sessionId, project, task: text, provider, intent })", send_block)
        self.assertIn("await deps.acceptRunResponse(r, sessionId);", send_block)
        self.assertIn("return true;", send_block)

        attach_start = HTML.index("async function attachCurrentChatToPickedProject")
        attach_end = HTML.index("function pushMsgToSession", attach_start)
        attach_block = HTML[attach_start:attach_end]
        self.assertIn("const sid = activeId;", attach_block)
        self.assertIn("if (!s || sessionProject(s)) return;", attach_block)
        self.assertIn("const draft = $('task').value.trim();", attach_block)
        self.assertIn("if (!data) return;", attach_block)
        self.assertIn("if (!attachSessionToProject(p.id, sid)) return;", attach_block)
        self.assertIn("await sendTaskFromSession(sid, draft, provider, () => clearDraftIfUnchanged(sid, draft));", attach_block)
        self.assertNotIn("/api/new_chat", attach_block)

        attach_session_start = HTML.index("function attachSessionToProject")
        attach_session_end = HTML.index("async function pickProject", attach_session_start)
        attach_session_block = HTML[attach_session_start:attach_session_end]
        self.assertIn("s.research = false;", attach_session_block)

        clear_start = COMPOSER_JS.index("function clearDraftIfUnchanged")
        clear_end = COMPOSER_JS.index("async function sendTaskFromSession", clear_start)
        clear_block = COMPOSER_JS[clear_start:clear_end]
        self.assertIn("if (activeId() !== sessionId || $('task').value.trim() !== draft) return;", clear_block)
        self.assertIn("$('task').value = '';", clear_block)

        send_click = COMPOSER_JS[COMPOSER_JS.index("async function sendActiveDraft()"):COMPOSER_JS.index("async function continueTask")]
        self.assertIn("await sendTaskFromSession(sessionId, task, provider, () => clearDraftIfUnchanged(sessionId, task));", send_click)
        self.assertNotIn("$('task').value = '';", send_click)

    def test_no_content_based_implementation_trigger_in_send_flow(self) -> None:
        self.assertNotIn("IMPLEMENTATION_REQUEST_RE", HTML)
        self.assertNotIn("implementationIntent", HTML)
        self.assertNotIn("detectImplementation", HTML)
        self.assertNotIn("startBuilding", HTML)

        send_start = COMPOSER_JS.index("async function sendActiveDraft()")
        send_end = COMPOSER_JS.index("async function continueTask", send_start)
        send_block = COMPOSER_JS[send_start:send_end]
        self.assertNotIn("RegExp", send_block)
        self.assertNotIn(".match(", send_block)
        self.assertNotIn(".includes(", send_block)

    def test_pagehide_flushes_pending_ui_state_before_beacon(self) -> None:
        page_start = UI_STATE_JS.index("window.addEventListener('pagehide'")
        page_end = UI_STATE_JS.index("async function restoreUiStateFromServer()", page_start)
        page_block = UI_STATE_JS[page_start:page_end]
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
            RENDER_JS,
        )
        self.assertIn("function toolRowEl(m, compact)", RENDER_JS)
        self.assertIn("function createToolGroup(kind)", RENDER_JS)
        self.assertIn("function standaloneToolEl(m)", RENDER_JS)
        self.assertIn("function appendToToolGroup(group, m)", RENDER_JS)
        self.assertIn("function appendOrFoldTool(chat, m)", RENDER_JS)
        self.assertIn("function foldCountLabel(kind, n)", RENDER_JS)

        # count labels pluralize correctly, incl. the irregular "searches".
        self.assertIn("search: ['search', 'searches'],", RENDER_JS)
        self.assertIn("read: ['file', 'files'],", RENDER_JS)
        self.assertNotIn("noun + (n === 1 ? '' : 's')", UI_SOURCE)

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
        standalone_start = RENDER_JS.index("function standaloneToolEl(m)")
        standalone_end = RENDER_JS.index("function appendToToolGroup", standalone_start)
        standalone_block = RENDER_JS[standalone_start:standalone_end]
        self.assertIn("div.className = 'msg tool';", standalone_block)
        self.assertIn("div.dataset.foldkind = m.kind;", standalone_block)
        self.assertIn("div.appendChild(toolRowEl(m, false));", standalone_block)

        # merge only into a trailing group or standalone row of the same kind.
        merge_start = RENDER_JS.index("function appendOrFoldTool(chat, m)")
        merge_end = RENDER_JS.index("window.CodeyRender = {", merge_start)
        merge_block = RENDER_JS[merge_start:merge_end]
        self.assertIn("const last = chat.lastElementChild;", merge_block)
        self.assertIn("last.dataset.foldkind === m.kind", merge_block)
        self.assertIn("last.replaceWith(group);", merge_block)
        self.assertIn("chat.appendChild(standaloneToolEl(m));", merge_block)

        append_start = RENDER_JS.index("function appendToToolGroup(group, m)")
        append_end = RENDER_JS.index("function appendOrFoldTool(chat, m)", append_start)
        append_block = RENDER_JS[append_start:append_end]
        self.assertIn("body.children.length", append_block)

        # converted groups default collapsed and toggle on click; state is not persisted.
        group_start = RENDER_JS.index("function createToolGroup(kind)")
        group_end = RENDER_JS.index("function standaloneToolEl(m)", group_start)
        group_block = RENDER_JS[group_start:group_end]
        self.assertIn("group.className = 'tool-group collapsed';", group_block)
        self.assertIn("group.dataset.foldkind = kind;", group_block)
        self.assertIn("summary.onclick = () => group.classList.toggle('collapsed');", group_block)
        self.assertNotIn("persist", group_block)

        # monochrome, cardless folding CSS: hidden body, chevron rotate, no colors.
        self.assertIn(".tool-line.compact { grid-template-columns: 1fr auto auto; }", STYLE_SOURCE)
        self.assertIn(".tool-group-body { display: none; padding-left: 20px; }", STYLE_SOURCE)
        self.assertIn(".tool-group:not(.collapsed) .tool-group-body { display: block; }", STYLE_SOURCE)
        self.assertIn(".tool-group:not(.collapsed) .tg-chevron { transform: rotate(90deg); }", STYLE_SOURCE)
        group_css_start = APP_CSS.index("/* ---------- tool group (read-only folding) ---------- */")
        group_css_end = APP_CSS.index("/* ---------- turn divider ---------- */", group_css_start)
        group_css = APP_CSS[group_css_start:group_css_end]
        self.assertNotIn("#", group_css.replace("var(--", ""))


if __name__ == "__main__":
    unittest.main()
