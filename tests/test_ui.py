from __future__ import annotations

import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.frontend_source import frontend_source_text

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "frontend"
ASSET_DIR = ROOT / "frontend" / "src"
HTML = (WEB_DIR / "index.html").read_text(encoding="utf-8")
DOCUMENT_HTML = HTML
APP_RUNTIME_SOURCE = (ASSET_DIR / "app/app_runtime.ts").read_text(encoding="utf-8")
def _source(name: str) -> str:
    # Structural checks read the compiler output for runtime shape and the
    # corresponding TypeScript source for stable statement boundaries.  The
    # source is included verbatim so formatting changes in the compiler do not
    # turn into false negatives; browser and Vitest tests remain the behavior
    # oracle.
    source_path = ASSET_DIR / f"{name.removesuffix('.ts')}.ts"
    raw = source_path.read_text(encoding="utf-8") if source_path.exists() else ""
    return f"{frontend_source_text(name)}\n{raw}"
GRAPH_JS = _source("research/research_graph")
RESEARCH_DRAWER_JS = _source("research/research_drawer")
RESEARCH_RUNS_JS = _source("research/research_runs")
CHANGES_DRAWER_JS = _source("workspace/changes_drawer")
LOCAL_CONTEXT_DRAWER_JS = _source("workspace/local_context_drawer")
RUN_DETAILS_JS = _source("runs/run_details")
RENDER_JS = _source("render/render")
PROVIDER_UI_JS = _source("providers/provider_ui")
SETTINGS_JS = _source("providers/settings")
UI_STATE_JS = _source("state/ui_state")
SSE_JS = _source("sse")
COMPOSER_JS = _source("conversation/composer")
CONVERSATION_UI_JS = _source("conversation/conversation_ui")
TOKENS_CSS = (ROOT / "frontend/public/assets/tokens.css").read_text(encoding="utf-8")
APP_CSS = (ROOT / "frontend/public/assets/app.css").read_text(encoding="utf-8")
STYLE_SOURCE = TOKENS_CSS + "\n" + APP_CSS
RUNTIME_SOURCES = tuple(path for path in sorted(ASSET_DIR.rglob("*.ts")) if not path.name.endswith((".test.ts", ".d.ts")))
JS_ASSETS = re.sub(
    r"\bimport type .*?;\s*|:\s*(?:any|string|number|boolean|unknown|Message|ResearchPayload|HTMLElement|ModuleDeps|Response|Element|RunData|Record<string,\s*RunData>)(?=\s*[,)=;])",
    "",
    "\n".join(frontend_source_text(path.relative_to(ASSET_DIR).as_posix()) for path in RUNTIME_SOURCES),
)
# Keep the source graph available for stable structural checks. Runtime
# behavior is exercised through the compiled bundle and browser/Vitest tests.
JS_ASSETS += "\n" + "\n".join(
    path.read_text(encoding="utf-8")
    for path in RUNTIME_SOURCES
)
# Behavioral assertions historically inspected the assembled page.  Keep that
# contract against the TypeScript source graph while the browser loads the
# compiled module entry from index.html.
HTML += "\n" + JS_ASSETS
UI_SOURCE = HTML + "\n" + STYLE_SOURCE + "\n" + JS_ASSETS


def _esbuild_command(root: Path) -> str | None:
    """Find the platform's esbuild launcher without assuming a Windows shell."""
    on_path = shutil.which("esbuild")
    if on_path:
        return on_path
    binary = "esbuild.cmd" if sys.platform == "win32" else "esbuild"
    candidate = root / "frontend" / "node_modules" / ".bin" / binary
    return str(candidate) if candidate.is_file() else None


class ProviderSelectorUiTests(unittest.TestCase):
    # Structural checks remain executable against the module source graph.

    def test_esbuild_resolution_uses_platform_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "frontend" / "node_modules" / ".bin" / "esbuild.cmd"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("", encoding="utf-8")
            with mock.patch.object(shutil, "which", return_value=None), mock.patch.object(sys, "platform", "win32"):
                self.assertEqual(_esbuild_command(root), str(launcher))

    def test_visible_ui_uses_english_document_language(self) -> None:
        self.assertIn('<html lang="en">', HTML)
        self.assertNotRegex(DOCUMENT_HTML, r"[\u4e00-\u9fff]")

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
        self.assertIn("Leave blank to keep saved key", SETTINGS_JS)
        self.assertNotIn("local-clear-api-key", HTML)
        self.assertNotIn("Clear saved key", HTML)
        self.assertNotIn("clear_api_key", HTML)
        self.assertIn("requestResponse('/api/local_provider',", PROVIDER_UI_JS)
        # Menu items are generated from the backend catalog (no static copy).
        self.assertNotIn('data-provider="deepseek"', HTML)
        self.assertIn("deepseek", UI_STATE_JS)
        self.assertIn("mimo", UI_STATE_JS)
        self.assertIn("qwen", UI_STATE_JS)
        self.assertIn("stepfun", UI_STATE_JS)
        self.assertIn("glm", UI_STATE_JS)
        self.assertIn("local", UI_STATE_JS)
        self.assertIn("buildProviderMenu", PROVIDER_UI_JS)
        self.assertIn("adoptBackendCatalog", PROVIDER_UI_JS)
        self.assertIn("setProviders", UI_STATE_JS)
        self.assertIn("requestResponse('/api/provider_catalog'", PROVIDER_UI_JS)
        self.assertIn("glm: 'GLM'", UI_STATE_JS)
        self.assertIn("local: 'Local'", UI_STATE_JS)
        self.assertIn('id="provider-dot"', HTML)
        self.assertIn("--ok-dot:", STYLE_SOURCE)
        self.assertIn(".dot.ok", STYLE_SOURCE)
        self.assertIn("let providerStatus", PROVIDER_UI_JS)
        self.assertIn("function refreshProviderStatus(immediate = false)", PROVIDER_UI_JS)
        self.assertIn("requestResponse('/api/providers')", PROVIDER_UI_JS)
        self.assertIn("providerStatus[id] ? 'ok' : ''", PROVIDER_UI_JS)
        self.assertIn("state === 'ready' || state === 'stale' ? 'ok'", PROVIDER_UI_JS)
        self.assertIn(".provider-item.active .check", STYLE_SOURCE)
        self.assertNotIn("providerAvailability(_id) { return 'ok'; }", UI_SOURCE)
        self.assertIn("providers/provider_ui", APP_RUNTIME_SOURCE)
        self.assertIn("state/ui_state", APP_RUNTIME_SOURCE)
        self.assertIn("conversation/composer", APP_RUNTIME_SOURCE)
        self.assertIn("ProviderUI.init({", HTML)
        self.assertIn("export const ProviderUI = {", PROVIDER_UI_JS)
        self.assertIn("function applyProviderStatus(rawProviders, isSSE = true", HTML)
        self.assertIn("ProviderUI.applyStatus(providers, isSSE)", HTML)
        self.assertIn("function refreshProviderStatus(immediate = false)", HTML)
        self.assertIn("ProviderUI.refreshStatus(immediate)", HTML)
        self.assertIn("../sse", APP_RUNTIME_SOURCE)
        self.assertIn("export const Sse = {", SSE_JS)
        self.assertIn("export const Composer = {", COMPOSER_JS)

    def test_provider_selector_orders_deepseek_mimo_stepfun_qwen_glm_local(self) -> None:
        # Order lives in the single ui_state.js fallback (backend is canonical).
        deepseek = UI_STATE_JS.index("deepseek")
        mimo = UI_STATE_JS.index("mimo")
        stepfun = UI_STATE_JS.index("stepfun")
        qwen = UI_STATE_JS.index("qwen")
        glm = UI_STATE_JS.index("'GLM'")
        local = UI_STATE_JS.index("'Local'")

        self.assertLess(deepseek, mimo)
        self.assertLess(mimo, stepfun)
        self.assertLess(stepfun, qwen)
        self.assertLess(qwen, glm)
        self.assertLess(glm, local)


    def test_context_handoff_stays_hidden(self) -> None:
        self.assertNotIn("fetch(\'/api/new_chat\'", APP_RUNTIME_SOURCE[APP_RUNTIME_SOURCE.index("function newSession("):APP_RUNTIME_SOURCE.index("async function forgetSessionState(")])
        self.assertNotIn("/compact", HTML)
        self.assertNotIn("context limit", HTML.lower().split("<dialog")[0])
        self.assertNotIn("context compression", HTML.lower())
        self.assertNotIn("handoff summary", HTML.lower())


    def test_local_context_is_in_the_right_workspace(self) -> None:
        self.assertNotIn('id="topbar-more"', HTML)
        self.assertNotIn('id="top-menu"', HTML)
        self.assertNotIn('>Clear messages<', HTML)
        self.assertIn("workspace.register('context', 'Local context'", APP_RUNTIME_SOURCE)
        self.assertIn('id="local-context-drawer"', HTML)
        self.assertIn('<strong>Local context</strong>', HTML)
        self.assertIn("workspace/local_context_drawer", APP_RUNTIME_SOURCE)
        self.assertIn("LocalContextDrawer.init({", HTML)
        self.assertIn("export const LocalContextDrawer = {", LOCAL_CONTEXT_DRAWER_JS)
        self.assertNotIn("sidebar", LOCAL_CONTEXT_DRAWER_JS.casefold())
        self.assertNotIn("Review context", UI_SOURCE)

    def test_local_context_ui_avoids_internal_terms(self) -> None:
        visible_source = HTML + "\n" + LOCAL_CONTEXT_DRAWER_JS
        for term in ("Ghost", "Affinity", "Hebbian", "Directive"):
            self.assertNotIn(term, visible_source)
        self.assertIn("Local context", visible_source)
        self.assertIn("Item type", visible_source)
        self.assertIn("Submit for review", visible_source)


    def test_run_details_style_uses_existing_design_tokens(self) -> None:
        start = APP_CSS.index(".run-details {")
        end = APP_CSS.index("/* ---------- changes card ---------- */", start)
        block = APP_CSS[start:end]

        self.assertIn("border-top: 1px solid var(--border-2)", block)
        self.assertIn("color: var(--muted)", block)
        self.assertIn("color: var(--text-dim)", block)
        self.assertIn(".run-details-row.warning .run-details-value { color: var(--text-dim); }", block)
        self.assertIn("font-size: calc(10.5px * var(--font-scale))", block)
        self.assertIn("font-size: calc(12px * var(--font-scale))", block)
        self.assertIn("letter-spacing: 1px", block)
        self.assertIn("text-transform: uppercase", block)
        for forbidden in ("box-shadow", "background:", "border-radius", "#", "rgba(", "var(--err-text)"):
            self.assertNotIn(forbidden, block)

    def test_disclosure_chevrons_share_the_same_brightness_tokens(self) -> None:
        process = APP_CSS[APP_CSS.index('.process-summary::after'):APP_CSS.index('.process-summary:hover {')]
        groups = APP_CSS[APP_CSS.index('.tool-group-summary .tg-chevron'):APP_CSS.index('.tool-group:not(.collapsed) .tg-chevron')]
        self.assertIn('background: var(--text-dim)', process)
        self.assertIn('background: var(--text-dim)', groups)
        self.assertIn("mask: url(\"/assets/chevron-down.svg\")", process)
        self.assertIn("mask: url('/assets/chevron-down.svg')", groups)

    def test_local_context_group_titles_follow_group_label_style(self) -> None:
        start = APP_CSS.index(".local-context-group-title")
        end = APP_CSS.index(".local-context-row", start)
        block = APP_CSS[start:end]
        self.assertIn("font-size: calc(10.5px * var(--font-scale))", block)
        self.assertIn("text-transform: uppercase", block)
        self.assertIn("letter-spacing: 1px", block)
        self.assertIn("color: var(--muted)", block)

    def test_local_context_empty_state_is_single_summary(self) -> None:
        self.assertIn("${data.items.length} items", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("${data.pending.length} pending", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("Submit for review", LOCAL_CONTEXT_DRAWER_JS)
        self.assertNotIn("No local context yet", LOCAL_CONTEXT_DRAWER_JS)

    def test_local_context_settings_are_visually_separated(self) -> None:
        start = APP_CSS.index(".local-context-settings")
        end = APP_CSS.index(".drawer-btn.arming", start)
        block = APP_CSS[start:end]
        self.assertIn("border-top: 1px solid var(--border-2)", block)

    def test_local_context_row_menu_is_keyboard_reachable_without_hover(self) -> None:
        start = APP_CSS.index(".local-context-more {")
        end = APP_CSS.index(".local-context-settings", start)
        self.assertNotIn("visibility: hidden", APP_CSS[start:end])

    def test_local_save_failure_keeps_popover_open(self) -> None:
        start = SETTINGS_JS.index("async function save()")
        block = SETTINGS_JS[start:start + 2400]
        self.assertIn("if (!r.ok || !data.ok) {", block)
        failure_at = block.index("if (!r.ok || !data.ok) {")
        success_at = block.index("deps.applyLocalMetadata")
        return_at = block.index("return;", failure_at)
        # Failure returns before applying metadata; saving a connection keeps
        # Settings open so the user can refresh and select models explicitly.
        self.assertLess(failure_at, return_at)
        self.assertLess(return_at, success_at)
        self.assertNotIn("close();", block)
        self.assertIn("local.context_error", SETTINGS_JS)


    def test_ghost_post_turn_warning_is_preserved_in_diagnostics(self) -> None:
        start = HTML.index("if (data.type === 'ghost_post_turn_warning')")
        end = HTML.index("if (data.type === 'teach_request')", start)
        block = HTML[start:end]
        self.assertIn("type: 'local_update_warning'", block)
        self.assertIn("Local memory update unavailable", block)
        self.assertNotIn("Local context", block)
        self.assertNotIn("Post-turn warning (", block)
        self.assertIn("local-health:", block)
        self.assertIn("this.deps.transport.eventKey(cursor", block)

    def test_shell_approval_preserves_command_identity_without_reason_section(self) -> None:
        self.assertIn("title.textContent = 'Run this command?'", UI_SOURCE)
        self.assertNotIn("${riskTitle || 'Shell command'}", UI_SOURCE)
        self.assertIn("full sha256", UI_SOURCE)
        self.assertIn("riskLabel: data.risk_label", HTML)
        self.assertIn("riskTitle: data.risk_title", HTML)
        self.assertIn("riskDetail: data.risk_detail", HTML)
        self.assertIn("commandSha256: data.command_sha256", HTML)
        self.assertIn("commandTruncated: !!data.command_truncated", HTML)

    def test_provider_status_is_quiet_and_refreshes_on_menu_open(self) -> None:
        open_block = PROVIDER_UI_JS[PROVIDER_UI_JS.index("function openMenu()"):PROVIDER_UI_JS.index("function applyProviderConfig")]
        self.assertIn("refreshProviderStatus();", open_block)
        self.assertIn("if (data.type === 'providers')", HTML)
        self.assertIn("applyProviderStatus(data.providers)", HTML)
        self.assertIn("providerUpdatedAt[item.id] > snapshotTime", PROVIDER_UI_JS)
        self.assertIn("providerUpdatedAt.local = Date.now()", PROVIDER_UI_JS)
        self.assertNotIn("fetchTime < lastAppliedSSETime", PROVIDER_UI_JS)
        self.assertIn("data.provider_failure.action === 'connect'", HTML)
        self.assertIn("applyProviderStatus([{ id: data.provider, available: false }])", HTML)
        self.assertIn("addSendError(sid, terminalKey, runId)", HTML)
        self.assertNotIn("Could not connect model", UI_SOURCE)
        self.assertNotIn("Open the model page", UI_SOURCE)
        self.assertNotIn("PlaywrightContextManager", UI_SOURCE)

    def test_provider_probe_failure_shows_a_quiet_menu_warning(self) -> None:
        self.assertIn('id="provider-probe-warning"', HTML)
        self.assertIn("Provider status unavailable", HTML)
        self.assertIn("setProbeWarning(!!data.probe_error)", PROVIDER_UI_JS)
        self.assertIn("setProbeWarning(true)", PROVIDER_UI_JS)
        self.assertIn(".provider-probe-warning", STYLE_SOURCE)
        self.assertIn("color: var(--muted)", STYLE_SOURCE)

    def test_deleting_last_session_preserves_selected_provider(self) -> None:
        source = _source("state/session_state_store")
        self.assertIn("const provider = this.currentProviderId()", source)
        self.assertIn("defaultSession(null, provider)", source)

    def test_topbar_shows_running_spinner(self) -> None:
        self.assertIn(".spinner", STYLE_SOURCE)
        self.assertIn('id="status"', HTML)
        self.assertIn("this.deps.transport.displayRunStatus('running')", _source("runs/run_event_coordinator"))
        self.assertIn("drawer-loading", STYLE_SOURCE)
        self.assertIn("class=\"spinner\"", CHANGES_DRAWER_JS)
        self.assertIn("spinner.className = 'spinner';", RESEARCH_DRAWER_JS)
        self.assertIn("spinner.className = 'spinner';", RUN_DETAILS_JS)
        self.assertIn("Loading...", LOCAL_CONTEXT_DRAWER_JS)

    def test_status_rows_use_continue_and_retry_links(self) -> None:
        self.assertIn(".link-btn", STYLE_SOURCE)
        self.assertIn("Continue", UI_SOURCE)
        self.assertIn("Retry", UI_SOURCE)
        self.assertIn("Resume", UI_SOURCE)

    def test_control_teaching_uses_plain_paused_status_row(self) -> None:
        self.assertIn("data.type === 'teach_request'", HTML)
        self.assertIn("Click the control in the model page", UI_SOURCE)
        self.assertIn("resumeTeaching", HTML)
        self.assertIn("/api/teach/resume", HTML)
        teach_start = UI_SOURCE.index("} else if (m.type === 'teach') {")
        teach_end = UI_SOURCE.index("} else if (m.type === 'err') {", teach_start)
        teach_block = UI_SOURCE[teach_start:teach_end]
        self.assertNotIn("DOM", teach_block)
        self.assertNotIn("selector", teach_block.lower())
        self.assertNotIn("override", teach_block.lower())



    def test_research_notes_load_in_one_batch(self) -> None:
        self.assertIn("/api/research/notes", RESEARCH_DRAWER_JS)
        self.assertIn("missing", RESEARCH_DRAWER_JS)
        self.assertNotIn("/api/research/note?id=", RESEARCH_DRAWER_JS)


    def test_agent_events_are_structured_instead_of_parsed_from_logs(self) -> None:
        self.assertIn("if (data.type === 'turn')", HTML)
        self.assertIn("if (data.type === 'tool')", HTML)
        self.assertIn("if (data.type === 'info')", HTML)
        turn_start = UI_SOURCE.index("if (data.type === 'turn')")
        turn_end = UI_SOURCE.index("if (data.type === 'tool_started')", turn_start)
        turn_block = UI_SOURCE[turn_start:turn_end]
        self.assertIn("note: (data.note || '').toString()", turn_block)
        render_start = UI_SOURCE.index("} else if (m.type === 'turn') {")
        render_end = UI_SOURCE.index("} else if (m.type === 'asst') {", render_start)
        render_block = UI_SOURCE[render_start:render_end]
        self.assertIn("const note = (m.note || '').toString().trim();", render_block)
        self.assertIn("label.textContent = `Turn ${m.n}${note ? ` ${note}` : ''}`;", render_block)
        self.assertNotIn("const turnMatch = line.match", HTML)
        self.assertNotIn("const toolMatch = line.match", HTML)
        self.assertNotIn("if (data.type === 'log')", HTML)
        self.assertNotIn("function handleLog", HTML)



    def test_composer_enter_sends_and_shift_enter_keeps_newline(self) -> None:
        self.assertIn('id="send-hint">Enter</span>', HTML)
        self.assertIn("e.key === 'Enter' && !e.shiftKey", COMPOSER_JS)
        self.assertIn("!e.isComposing", COMPOSER_JS)
        self.assertIn("e.keyCode !== 229", COMPOSER_JS)
        self.assertNotIn("e.key === 'Enter' && (e.ctrlKey || e.metaKey)", UI_SOURCE)
        self.assertNotIn('id="send-hint">Ctrl ↵</span>', HTML)

    def test_routine_failures_do_not_use_native_alerts(self) -> None:
        self.assertNotIn("alert(", HTML)
        self.assertIn("error: 'Could not add project'", HTML)
        self.assertIn("setStatus(data.error, 'err')", APP_RUNTIME_SOURCE)
        self.assertIn("Run this command?", UI_SOURCE)
        self.assertIn("Decision could not be confirmed", UI_SOURCE)
        self.assertIn("approvalController.submit(id, approved, scope)", HTML)
        self.assertNotIn("Failed to pick folder", HTML)
        self.assertNotIn("Shell approval failed", HTML)
        self.assertNotIn("approveShell", HTML)

    def test_review_status_is_quiet_and_has_no_switch(self) -> None:
        self.assertIn("data.type === 'review'", HTML)
        source = _source("runs/run_event_coordinator")
        block = source[source.index("if (data.type === 'review')"):]
        for field in ("type: 'review'", "text: data.text", "sessionId: sid", "runId,", "this.deps.transport.eventKey(cursor)"):
            self.assertIn(field, block)
        self.assertIn("statusRow('Review'", UI_SOURCE)
        review_start = UI_SOURCE.index("} else if (m.type === 'review') {")
        review_end = UI_SOURCE.index("} else if (m.type === 'changes') {", review_start)
        review_block = UI_SOURCE[review_start:review_end]
        self.assertNotIn("RunDetails", review_block)
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
        self.assertIn("type: 'asst'", reply_block)
        self.assertIn("text: data.text", reply_block)
        self.assertIn("runId", reply_block)
        self.assertIn("eventKey: answerKey", reply_block)
        self.assertIn("terminalAnswerMessage", reply_block)
        self.assertIn("final || { type: 'asst'", reply_block)

    def test_stopped_terminal_event_renders_status_row(self) -> None:
        done_start = HTML.index("if (data.type === 'task_done')")
        done_end = HTML.index("if (data.type === 'shell_request')", done_start)
        done_block = HTML[done_start:done_end]

        self.assertIn("reason === 'stopped'", done_block)
        self.assertIn("Stopped after ${turns} turn", done_block)
        self.assertIn("type: 'pause', text: label", done_block)

    def test_error_terminal_event_renders_error_row(self) -> None:
        done_start = HTML.index("if (data.type === 'task_done')")
        done_end = HTML.index("if (data.type === 'shell_request')", done_start)
        done_block = HTML[done_start:done_end]

        self.assertIn("reason === 'error'", done_block)
        self.assertIn("const text = summary || 'Task failed';", done_block)
        for field in ("type: 'err'", "text,", "sessionId: sid", "runId,", "eventKey: terminalKey"):
            self.assertIn(field, done_block)

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
        self.assertIn("data.mode === 'git' ? 'Working tree' : 'Snapshot'", CHANGES_DRAWER_JS)
        self.assertIn("/api/changes/restore", HTML)
        self.assertNotIn("Reading git diff", UI_SOURCE)
        self.assertIn("workspace/changes_drawer", APP_RUNTIME_SOURCE)
        self.assertIn("ChangesDrawer.init({", HTML)
        self.assertIn("export const ChangesDrawer = {", CHANGES_DRAWER_JS)

    def test_changes_drawer_hides_diff_metadata_lines(self) -> None:
        self.assertIn('class="diff-line add"><span class="ln"', CHANGES_DRAWER_JS)
        self.assertIn('class="diff-line del"><span class="ln"', CHANGES_DRAWER_JS)
        self.assertIn("line.startsWith('diff --git')", CHANGES_DRAWER_JS)
        self.assertNotIn("line.startsWith('@@')) cls += ' hunk'", UI_SOURCE)

    def test_composer_holds_model_picker_with_green_dot(self) -> None:
        self.assertIn('class="provider-chooser"', HTML)
        self.assertIn('class="provider-button"', HTML)
        self.assertIn('class="provider-menu"', HTML)
        # Menu items are built by provider_ui.js from the backend catalog;
        # HTML holds only the container + warning (no second static copy).
        self.assertIn("buildProviderMenu", PROVIDER_UI_JS)
        self.assertIn("provider-item", PROVIDER_UI_JS)
        self.assertNotIn('data-provider="deepseek"', HTML)

    def test_composer_uses_default_browser_capabilities_without_a_toggle(self) -> None:
        self.assertNotIn('id="ctx-browser"', APP_RUNTIME_SOURCE)
        self.assertNotIn("requested_capabilities", COMPOSER_JS)
        for grant in ("browser.observe", "browser.interact", "browser.effect"):
            self.assertNotIn(grant, COMPOSER_JS)


    def test_new_chat_is_blank_without_welcome_or_example_cards(self) -> None:
        self.assertNotIn("Send a message to start.", CONVERSATION_UI_JS)
        self.assertNotIn("<h1>Codey</h1>", CONVERSATION_UI_JS)
        self.assertIn("empty-conversation", CONVERSATION_UI_JS)
        self.assertIn("main.composer-animating .composer-wrap", APP_CSS)
        self.assertNotIn("打个招呼", HTML)
        self.assertNotIn("生成 snake.py", HTML)
        self.assertNotIn("写 README", HTML)
        self.assertNotIn("找 bug", HTML)





    def test_markdown_stays_monochrome_without_syntax_highlighting(self) -> None:
        md_css_start = APP_CSS.index("assistant markdown")
        md_css_end = APP_CSS.index("tool line", md_css_start)
        md_css = APP_CSS[md_css_start:md_css_end]
        self.assertNotIn("#", md_css.replace("var(--", ""))
        self.assertNotIn("hljs", UI_SOURCE)
        self.assertNotIn("highlight.js", UI_SOURCE)

    def test_code_blocks_have_quiet_copy_button(self) -> None:
        self.assertIn("function codeCopyButton(value)", RENDER_JS)
        self.assertIn("parser.renderer.rules.fence", _source("render/markdown"))
        self.assertIn("className = 'code-copy'", RENDER_JS)
        self.assertIn("aria-label', 'Copy code'", RENDER_JS)
        self.assertIn("escape(token.content", _source("render/markdown"))
        self.assertIn(".md-code:hover .code-copy", STYLE_SOURCE)
        self.assertIn("await copyText(value)", RENDER_JS)

    def test_activity_rows_share_columns_and_icon_only_copy_actions(self) -> None:
        self.assertIn("grid-template-columns: 12px minmax(0, 1fr) 12px minmax(0, 2fr)", STYLE_SOURCE)
        self.assertIn("grid-template-columns: 12px auto minmax(0, 1fr) 12px", STYLE_SOURCE)
        self.assertIn(".tool-line .tl-kind { display: none; }", STYLE_SOURCE)
        self.assertIn("className = 'activity-row command-summary'", _source("runs/command_card"))
        self.assertIn("createIconSvg('copy')", RENDER_JS)
        self.assertNotIn("Result available", RENDER_JS)
        # Keep the glyph check scoped to activity-row styles. Other UI
        # disclosures may intentionally use a textual fallback arrow.
        tool_activity_css = APP_CSS[
            APP_CSS.index("/* ---------- tool line ---------- */") : APP_CSS.index(
                "/* ---------- status rows (done / pause / limit / error / changes) ---------- */"
            )
        ]
        command_activity_css = APP_CSS[
            APP_CSS.index(".command-card {") : APP_CSS.index(".command-panel {")
        ]
        activity_css = tool_activity_css + command_activity_css
        self.assertNotIn("content: '›'", activity_css)
        self.assertNotIn("content: '⌄'", activity_css)


    def test_streaming_events_stay_debounced_while_user_actions_flush(self) -> None:
        source = (ASSET_DIR / "state/session_state_store.ts").read_text(encoding="utf8")
        add_block = source[source.index("  addMessage("):source.index("  replaceMessage(")]
        self.assertIn("this.deps.ui.persist();", add_block)
        self.assertNotIn("persistNow(", add_block)
        push_block = source[source.index("  pushMessage("):source.index("  addMessage(")]
        self.assertIn("this.deps.ui.persistNow();", push_block)
        self.assertNotIn("this.deps.ui.persist();", push_block)
        self.assertIn("sessionState.addMessage(sid, m)", HTML)
        self.assertIn("sessionState.pushMessage(sid, m)", HTML)
        events = (ASSET_DIR / "runs/run_event_coordinator.ts").read_text(encoding="utf8")
        done_block = events[events.index("if (data.type === 'task_done')"):events.index("if (data.type === 'shell_request')")]
        self.assertIn("this.deps.ui.flush();", done_block)

    def test_discrete_session_actions_flush_immediately(self) -> None:
        source = (ASSET_DIR / "state/session_state_store.ts").read_text(encoding="utf8")
        switch_block = source[source.index("  switchSession("):source.index("  newSession(")]
        new_block = source[source.index("  newSession("):source.index("  openProject(")]
        self.assertIn("this.deps.ui.persistNow();", switch_block)
        self.assertIn("this.deps.ui.persistNow();", new_block)
        prov_start = COMPOSER_JS.index("function setActiveProvider(id)")
        prov_end = COMPOSER_JS.index("function clearDraftIfUnchanged", prov_start)
        self.assertIn("persistActiveNow();", COMPOSER_JS[prov_start:prov_end])
        project_block = source[source.index("  ensureProject("):source.index("  attachSessionToProject(")]
        self.assertIn("this.deps.ui.persist();", project_block)


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
        self.assertIn("function cancelRename()", _source("conversation/sidebar"))

        # Enter commits, Escape cancels, blur commits (avoids losing the edit).
        input_start = HTML.index("function renameInputEl(value, commit, cancel)")
        input_end = HTML.index("function focusRenameInput()", input_start)
        input_block = HTML[input_start:input_end]
        self.assertIn("e.key === 'Enter'", input_block)
        self.assertIn("finish(commit, input.value)", input_block)
        self.assertIn("e.key === 'Escape'", input_block)
        self.assertIn("finish(cancel, undefined)", input_block)
        self.assertIn("input.onblur = () => finish(commit, input.value);", input_block)
        # settled guard prevents a double commit from cancel->render->blur.
        self.assertIn("let settled = false;", input_block)
        # clicks inside the input must not bubble to the row switch/open handlers.
        self.assertIn("input.onmousedown = (e) => e.stopPropagation();", input_block)

        # rename entry points arm inline editing rather than prompting.
        sidebar_source = _source("conversation/sidebar")
        self.assertIn("sidebar.beginSessionRename(id)", HTML)
        self.assertIn("sidebar.beginProjectRename(id)", HTML)
        rs_start = sidebar_source.index("function beginSessionRename(id)")
        rs_end = sidebar_source.index("function beginProjectRename(id)", rs_start)
        rs_block = sidebar_source[rs_start:rs_end]
        self.assertIn("editingSessionId = id;", rs_block)
        self.assertIn("focusRenameInput();", rs_block)
        rp_start = sidebar_source.index("function beginProjectRename(id)")
        rp_end = sidebar_source.index("function cancelRename()", rp_start)
        rp_block = sidebar_source[rp_start:rp_end]
        self.assertIn("editingProjectId = id;", rp_block)
        self.assertIn("focusRenameInput();", rp_block)
        # committing a session rename still clamps title length (unchanged shape).
        self.assertIn("session.title = title.slice(0, 80);", _source("state/session_state_store"))






if __name__ == "__main__":
    unittest.main()
