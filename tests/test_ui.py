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
def _source(name: str) -> str:
    # Structural checks read the compiler output for runtime shape and the
    # corresponding TypeScript source for stable statement boundaries.  The
    # source is included verbatim so formatting changes in the compiler do not
    # turn into false negatives; browser and Vitest tests remain the behavior
    # oracle.
    source_path = ASSET_DIR / f"{name.removesuffix('.ts')}.ts"
    raw = source_path.read_text(encoding="utf-8") if source_path.exists() else ""
    return f"{frontend_source_text(name)}\n{raw}"
GRAPH_JS = _source("research_graph")
RESEARCH_DRAWER_JS = _source("research_drawer")
RESEARCH_RUNS_JS = _source("research_runs")
CHANGES_DRAWER_JS = _source("changes_drawer")
LOCAL_CONTEXT_DRAWER_JS = _source("local_context_drawer")
RUN_DETAILS_JS = _source("run_details")
RENDER_JS = _source("render")
PROVIDER_UI_JS = _source("provider_ui")
SETTINGS_JS = _source("settings")
UI_STATE_JS = _source("ui_state")
SSE_JS = _source("sse")
COMPOSER_JS = _source("composer")
CONVERSATION_UI_JS = _source("conversation_ui")
TOKENS_CSS = (ROOT / "frontend/public/assets/tokens.css").read_text(encoding="utf-8")
APP_CSS = (ROOT / "frontend/public/assets/app.css").read_text(encoding="utf-8")
STYLE_SOURCE = TOKENS_CSS + "\n" + APP_CSS
RUNTIME_SOURCES = tuple(path for path in sorted(ASSET_DIR.glob("*.ts")) if not path.name.endswith(".test.ts"))
JS_ASSETS = re.sub(
    r"\bimport type .*?;\s*|:\s*(?:any|string|number|boolean|unknown|Message|ResearchPayload|HTMLElement|ModuleDeps|Response|Element|RunData|Record<string,\s*RunData>)(?=\s*[,)=;])",
    "",
    "\n".join(frontend_source_text(path.stem) for path in RUNTIME_SOURCES),
)
JS_ASSETS += "\n" + _source("legacy-runtime")
# Keep the source graph available for stable structural checks. Runtime
# behavior is exercised through the compiled bundle and browser/Vitest tests.
JS_ASSETS += "\n" + "\n".join(
    (ASSET_DIR / path.name).read_text(encoding="utf-8")
    for path in RUNTIME_SOURCES
    if path.name != "legacy-runtime.ts"
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
        self.assertIn("Leave blank to keep saved key", SETTINGS_JS)
        self.assertNotIn("local-clear-api-key", HTML)
        self.assertNotIn("Clear saved key", HTML)
        self.assertNotIn("clear_api_key", HTML)
        self.assertIn("fetch('/api/local_provider',", PROVIDER_UI_JS)
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
        self.assertIn("fetch('/api/provider_catalog'", PROVIDER_UI_JS)
        self.assertIn("glm: 'GLM'", UI_STATE_JS)
        self.assertIn("local: 'Local'", UI_STATE_JS)
        self.assertIn('id="provider-dot"', HTML)
        self.assertIn("--ok-dot:", STYLE_SOURCE)
        self.assertIn(".dot.ok", STYLE_SOURCE)
        self.assertIn("let providerStatus", PROVIDER_UI_JS)
        self.assertIn("function refreshProviderStatus(immediate = false)", PROVIDER_UI_JS)
        self.assertIn("fetch('/api/providers')", PROVIDER_UI_JS)
        self.assertIn("providerStatus[id] ? 'ok' : ''", PROVIDER_UI_JS)
        self.assertIn("state === 'ready' || state === 'stale' ? 'ok'", PROVIDER_UI_JS)
        self.assertIn(".provider-item.active .check", STYLE_SOURCE)
        self.assertNotIn("providerAvailability(_id) { return 'ok'; }", UI_SOURCE)
        self.assertIn("./provider_ui", (ROOT / "frontend/src/main.ts").read_text(encoding="utf-8"))
        self.assertIn("./ui_state", (ROOT / "frontend/src/main.ts").read_text(encoding="utf-8"))
        self.assertIn("./composer", (ROOT / "frontend/src/main.ts").read_text(encoding="utf-8"))
        self.assertIn("runtime.ProviderUI.init({", HTML)
        self.assertIn("runtime.ProviderUI = {", PROVIDER_UI_JS)
        self.assertIn("function applyProviderStatus(providers, isSSE = true)", HTML)
        self.assertIn("runtime.ProviderUI.applyStatus(providers, isSSE)", HTML)
        self.assertIn("function refreshProviderStatus(immediate = false)", HTML)
        self.assertIn("runtime.ProviderUI.refreshStatus(immediate)", HTML)
        self.assertIn("./sse", (ROOT / "frontend/src/main.ts").read_text(encoding="utf-8"))
        self.assertIn("runtime.Sse = {", SSE_JS)
        self.assertIn("runtime.Composer = {", COMPOSER_JS)

    def test_boot_catalog_never_runs_the_availability_probe(self) -> None:
        start = PROVIDER_UI_JS.index("async function adoptBackendCatalog()")
        end = PROVIDER_UI_JS.index("async function", start + 1)
        body = PROVIDER_UI_JS[start:end]
        self.assertIn("fetch('/api/provider_catalog'", body)
        self.assertNotIn("fetch('/api/providers')", body)
        # /api/providers stays on the async refresh path only.
        self.assertIn("fetch('/api/providers')", PROVIDER_UI_JS)
        self.assertIn("await runtime.ProviderUI.adoptCatalog()", HTML)

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
        self.assertIn("JSON.stringify({ session_id: s.id })", HTML)
        self.assertNotIn("/compact", HTML)
        self.assertNotIn("context limit", HTML.lower().split("<dialog")[0])
        self.assertNotIn("context compression", HTML.lower())
        self.assertNotIn("handoff summary", HTML.lower())


    def test_local_context_entry_is_quiet_top_menu_item(self) -> None:
        self.assertNotIn('<button data-act="rename">Rename chat</button>', HTML)
        local = HTML.index('<button data-act="local-context">Local context</button>')
        clear = HTML.index('<button data-act="clear" class="danger"')

        self.assertLess(local, clear)
        self.assertIn('id="local-context-drawer"', HTML)
        self.assertIn('<strong>Local context</strong>', HTML)
        self.assertIn("./local_context_drawer", (ROOT / "frontend/src/main.ts").read_text(encoding="utf-8"))
        self.assertIn("runtime.LocalContextDrawer.init({", HTML)
        self.assertIn("runtime.LocalContextDrawer = {", LOCAL_CONTEXT_DRAWER_JS)
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

    def test_local_context_row_menu_is_keyboard_reachable_without_hover(self) -> None:
        start = APP_CSS.index(".local-context-more {")
        end = APP_CSS.index(".local-context-settings", start)
        self.assertNotIn("visibility: hidden", APP_CSS[start:end])

    def test_drawer_opening_is_mutually_exclusive(self) -> None:
        self.assertIn("function closeOtherDrawers(active)", HTML)
        self.assertIn("if (active !== 'changes') closeChangesDrawer();", HTML)
        self.assertIn("if (active !== 'research') runtime.ResearchDrawer.close();", HTML)
        self.assertIn("if (active !== 'local_context') closeLocalContextDrawer();", HTML)
        self.assertIn("deps.closeOtherDrawers('changes')", CHANGES_DRAWER_JS)
        self.assertIn("deps.closeOtherDrawers('research')", RESEARCH_DRAWER_JS)
        self.assertIn("deps.closeOtherDrawers('local_context')", LOCAL_CONTEXT_DRAWER_JS)







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
        self.assertIn("eventKey: sseEventKey(data", block)

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
        open_block = PROVIDER_UI_JS[PROVIDER_UI_JS.index("function openMenu()"):PROVIDER_UI_JS.index("function applyRecommended")]
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
        self.assertIn("const fallbackProvider = currentProviderId()", HTML)
        self.assertIn("defaultSession(null, fallbackProvider)", HTML)

    def test_topbar_shows_running_spinner(self) -> None:
        self.assertIn(".spinner", STYLE_SOURCE)
        self.assertIn('id="status"', HTML)
        self.assertIn("runtime.Sse.displayRunStatus('running')", HTML)
        self.assertIn("drawer-loading", STYLE_SOURCE)
        self.assertIn("class=\"spinner\"", CHANGES_DRAWER_JS)
        self.assertIn("spinner.className = 'spinner';", RESEARCH_DRAWER_JS)
        self.assertIn("spinner.className = 'spinner';", RUN_DETAILS_JS)
        self.assertIn("drawer-loading", LOCAL_CONTEXT_DRAWER_JS)
        self.assertIn("class=\"spinner\"", LOCAL_CONTEXT_DRAWER_JS)

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
        self.assertIn("setStatus('Could not add project', 'err')", HTML)
        self.assertIn("Run this command?", UI_SOURCE)
        self.assertIn("Decision could not be confirmed", UI_SOURCE)
        self.assertIn("approvalController.submit(id, approved)", HTML)
        self.assertNotIn("Failed to pick folder", HTML)
        self.assertNotIn("Shell approval failed", HTML)
        self.assertNotIn("approveShell", HTML)

    def test_review_status_is_quiet_and_has_no_switch(self) -> None:
        self.assertIn("data.type === 'review'", HTML)
        self.assertIn(
            "{ type: 'review', text: data.text, sessionId: sid, runId, eventKey: sseEventKey(data) }",
            UI_SOURCE,
        )
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
        self.assertIn("type: 'asst', text: data.text, runId, eventKey: answerKey", reply_block)

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
        self.assertIn("type: 'err', text, sessionId: sid, runId, eventKey: terminalKey", done_block)

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
        self.assertIn("./changes_drawer", (ROOT / "frontend/src/main.ts").read_text(encoding="utf-8"))
        self.assertIn("runtime.ChangesDrawer.init({", HTML)
        self.assertIn("runtime.ChangesDrawer = {", CHANGES_DRAWER_JS)

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


    def test_welcome_keeps_status_copy_without_example_cards(self) -> None:
        self.assertIn("Send a message to start.", CONVERSATION_UI_JS)
        self.assertIn("<h1>Codey</h1>", CONVERSATION_UI_JS)
        self.assertNotIn("打个招呼", HTML)
        self.assertNotIn("生成 snake.py", HTML)
        self.assertNotIn("写 README", HTML)
        self.assertNotIn("找 bug", HTML)





    def test_chunked_render_keeps_stateful_blocks_and_cancel(self) -> None:
        # A >120-line fenced block must render in one idle pass, never as
        # independently parsed 120-line slices (fence/list/quote state would
        # fork). Queued work still supports cancellation by its caller.
        self.assertIn("function hasStatefulBlocks(text)", RENDER_JS)
        self.assertIn("/^\\s*```/m.test(value)", RENDER_JS)
        self.assertIn("/^\\s*>\\s?/m.test(value)", RENDER_JS)
        self.assertIn("function renderMarkdownChunked(container, text, done, isCurrent)", RENDER_JS)
        self.assertIn("if (!alive()) return;", RENDER_JS)
        self.assertIn("scheduleIdleChunk(() => {", RENDER_JS)
        self.assertIn("if (hasStatefulBlocks(value)) {", RENDER_JS)
        self.assertIn("while (scratch.firstChild) container.appendChild(scratch.firstChild);", RENDER_JS)

    def test_markdown_stays_monochrome_without_syntax_highlighting(self) -> None:
        md_css_start = APP_CSS.index("assistant markdown")
        md_css_end = APP_CSS.index("tool line", md_css_start)
        md_css = APP_CSS[md_css_start:md_css_end]
        self.assertNotIn("#", md_css.replace("var(--", ""))
        self.assertNotIn("hljs", UI_SOURCE)
        self.assertNotIn("highlight.js", UI_SOURCE)

    def test_code_blocks_have_quiet_copy_button(self) -> None:
        self.assertIn("function addCodeCopyButton(pre, text)", RENDER_JS)
        self.assertIn("function appendCodeBlock(container, code, language", RENDER_JS)
        self.assertIn("className = 'code-copy'", RENDER_JS)
        self.assertIn("aria-label', 'Copy code'", RENDER_JS)
        self.assertIn("el.textContent = code", RENDER_JS)
        self.assertIn(".md-code:hover .code-copy", STYLE_SOURCE)
        self.assertIn("await copyText(value)", RENDER_JS)


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
        self.assertIn("finish(cancel, undefined)", input_block)
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






    def test_provider_shrink_and_default_change_applies_live(self) -> None:
        # Executable behavior lock (not just source strings): simulate boot with
        # two providers, then a post-boot catalog that removes one and flips the
        # default. Harness completeness is asserted in Python (runs everywhere);
        # Node execution is skipped where node is unavailable.
        import shutil
        import subprocess
        import tempfile

        harness = r"""
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const webDir = process.argv[2];
function loadAsset(name) {
  return fs.readFileSync(path.join(webDir, name), 'utf8');
}
// Fake DOM that truly records provider menu buttons.
const menuButtons = [];
function menuButtonCount() {
  return fakeMenu.querySelectorAll('.provider-item').length;
}
function makeElement(tag) {
  const el = {
    tag, children: [],
    className: '', dataset: {}, textContent: '', innerHTML: '',
    onclick: null, disabled: false, value: '', style: {},
    setAttribute: () => {},
    setSelectionRange: () => {},
    append: (...kids) => { el.children.push(...kids); },
    appendChild: (kid) => { el.children.push(kid); return kid; },
    replaceChildren: (...kids) => { el.children = kids; },
    remove: () => {
      const i = menuButtons.indexOf(el);
      if (i >= 0) menuButtons.splice(i, 1);
    },
    querySelector: (sel) => el.children.find(child => child.className === sel.slice(1)) || { className: '', textContent: '' },
    querySelectorAll: () => [],
    classList: { add: () => {}, remove: () => {}, toggle: () => {}, contains: () => false },
    addEventListener: () => {},
    closest: () => null,
    contains: () => false,
    click: () => {},
  };
  return el;
}
const fakeMenu = {
  querySelector: () => fakeSearch,
  setAttribute: () => {},
  addEventListener: () => {},
  querySelectorAll: (sel) => {
    if (sel === '.provider-item') return menuButtons.slice();
    return [];
  },
  insertBefore: (btn) => { menuButtons.push(btn); },
  appendChild: (btn) => { menuButtons.push(btn); },
  classList: { remove: () => {}, toggle: () => {}, contains: () => false },
  contains: () => false,
};
const fakeSearch = { value: '', hidden: false, focus: () => {} };
const fakeButtons = {};
function fakeButton() { return { onclick: null, disabled: false, setAttribute: () => {}, classList: { add: () => {}, remove: () => {}, toggle: () => {} } }; }
const fakePop = { classList: { add: () => {}, remove: () => {}, contains: () => false }, contains: () => false, setAttribute: () => {} };
global.document = {
  createElement: (tag) => makeElement(tag),
  querySelectorAll: (sel) => {
    if (sel === '.provider-item') return menuButtons.slice();
    return [];
  },
  getElementById: (id) => {
    if (id === 'provider-menu') return fakeMenu;
    if (id === 'provider-probe-warning') return null;
    if (id === 'provider-button') return fakeButtons['provider-button'] || (fakeButtons['provider-button'] = fakeButton());
    if (id === 'provider-name') return { textContent: '' };
    if (id === 'provider-dot') return { className: '' };
    if (['effort-button','effort-name','effort-chooser','effort-menu'].includes(id)) return fakeButtons[id] || (fakeButtons[id] = makeElement('button'));
    if (id === 'local-config-pop') return fakePop;
    if (id === 'local-config-close') return fakeButtons['local-config-close'] || (fakeButtons['local-config-close'] = fakeButton());
    if (id === 'local-config-save') return fakeButtons['local-config-save'] || (fakeButtons['local-config-save'] = fakeButton());
    if (id === 'task') return { value: '', style: {}, disabled: false, scrollHeight:40, setSelectionRange: () => {}, addEventListener: () => {}, click: () => {} };
    if (id === 'composer-context') return { onclick: null, addEventListener: () => {} };
    if (id === 'send' || id === 'stop') return { onclick: null, disabled: false, style: {}, setAttribute: () => {}, click: () => {} };
    if (id === 'send-hint') return { textContent: '' };
    if (id === 'model-menu-empty' || id === 'model-notice') return fakeButtons[id] || (fakeButtons[id] = makeElement('div'));
    return null;
  },
  addEventListener: () => {},
};
global.localStorage = { getItem: () => null, setItem: () => {} };
global.__TEST_BRIDGE__ = true;
    global.window = {__TEST_BRIDGE__:true, addEventListener:()=>{}, Settings:{init:()=>{}, open:()=>{}, close:()=>{}}, Requests:{refreshActions:()=>{}}};
global.navigator = {};
global.HTMLElement = function HTMLElement() {};
global.HTMLButtonElement = function HTMLButtonElement() {};
const uiStateSrc = loadAsset('assets/ui_state.js');
const providerUiSrc = loadAsset('assets/provider_ui.js');
const composerSrc = loadAsset('assets/composer.js');
vm.runInThisContext(loadAsset('assets/models.js'));
runtime.Models.applySettings({preferences:{revision:0,sources:{websites:{enabled:true,models:['deepseek','mimo']}}},
  sources:[{id:'websites',label:'Websites',models:[{id:'deepseek',name:'DeepSeek'},{id:'mimo',name:'MiMo'}]}],in_use:[]});
vm.runInThisContext(uiStateSrc);
vm.runInThisContext(providerUiSrc);
vm.runInThisContext(composerSrc);
async function main() {
  // Boot with two providers, default deepseek.
  runtime.UiState.setProviders(['deepseek', 'mimo'], { deepseek: 'DeepSeek', mimo: 'MiMo' }, 'deepseek');
  if (runtime.UiState.PROVIDERS.length !== 2) throw new Error('boot catalog not applied');
  if (runtime.UiState.DEFAULT_PROVIDER !== 'deepseek') throw new Error('boot default not applied');
  // Init provider_ui with live references (as index.html does).
  const PROVIDERS = runtime.UiState.PROVIDERS;
  const PROVIDER_LABELS = runtime.UiState.PROVIDER_LABELS;
  let current = 'mimo';
  runtime.ProviderUI.init({
    $: (id) => global.document.getElementById(id),
    escapeHtml: (s) => String(s),
    PROVIDERS, PROVIDER_LABELS,
    DEFAULT_PROVIDER: runtime.UiState.DEFAULT_PROVIDER,
    currentProviderId: () => current,
    setActiveProvider: (id) => { current = id; },
  });
  if (menuButtonCount() !== 2) throw new Error('boot menu must have 2 provider-item buttons, got ' + menuButtonCount());
  // Post-boot catalog: remove mimo via applyConfig (not direct setProviders).
  const shrunk = runtime.ProviderUI.applyConfig({
    providers: [{ id: 'deepseek', label: 'DeepSeek' }],
    default: 'deepseek',
  });
  if (shrunk !== true) throw new Error('shrink must rebuild menu (returned false)');
  if (runtime.UiState.PROVIDERS.length !== 1) throw new Error('state array did not shrink');
  if (runtime.UiState.PROVIDERS[0] !== 'deepseek') throw new Error('wrong survivor');
  if (menuButtonCount() !== 1) throw new Error('menu buttons did not shrink, got ' + menuButtonCount());
  if (!menuButtons[0].dataset || menuButtons[0].dataset.provider !== 'deepseek') throw new Error('menu button has wrong provider');
  // Default flip via applyConfig: re-add mimo with new default mimo.
  const flipped = runtime.ProviderUI.applyConfig({
    providers: [{ id: 'deepseek', label: 'DeepSeek' }, { id: 'mimo', label: 'MiMo' }],
    default: 'mimo',
  });
  if (flipped !== true) throw new Error('default flip must rebuild menu (returned false)');
  if (runtime.UiState.DEFAULT_PROVIDER !== 'mimo') throw new Error('default flip not applied');
  if (menuButtonCount() !== 2) throw new Error('menu buttons must be 2 after flip, got ' + menuButtonCount());
  // Composer live fallback: no snapshot dep; stale removed-id must resolve to live mimo.
  const composerProviders = runtime.UiState.PROVIDERS;
  const liveSession = { id: 's1', provider: 'removed-id', messages: [
    { type: 'shell_approval_status', approval: {continuation:'started', continuation_run_id:'older-approval-run'} },
    { type: 'user', attempts: [{ runId: 'source-run-1' }] },
  ], draft: {text:'',start:0,end:0,direction:'none',revision:0} };
  let syncedTo = null;
  const runBodies = [];
  global.fetch = async (url, opts) => {
    runBodies.push({ url, body: JSON.parse(opts.body) });
    return { status: 200, ok: true, json: async () => ({}) };
  };
  runtime.ShellApprovals = {continueSession: async () => false};
  runtime.Composer.init({
    $: (id) => global.document.getElementById(id),
    PROVIDERS: composerProviders,
    getRunningSessionId: () => null,
    getActiveId: () => 's1',
    activeSession: () => liveSession,
    findSession: () => liveSession,
    sessionProject: (s) => ({ path: '/tmp/proj' }),
    sessionProjectPath: () => '/tmp/proj',
    currentIntentForSession: () => 'project',
    currentProviderId: () => liveSession.provider,
    persistActive: () => {},
    persistActiveNow: () => {},
    updateComposerContext: () => {},
    renderChat: () => {},
    pushMsgToSession: () => {},
    addToSession: () => {},
    addSendError: () => { throw new Error('addSendError must not run on ok'); },
    acceptRunResponse: async () => {},
    syncProviderUI: (id) => { syncedTo = id; },
    openLocalProviderConfig: () => {},
    projectPickerBusy: () => false,
    attachCurrentChatToPickedProject: () => {},
  });
  runtime.Composer.setActiveProvider('removed-id');
  if (liveSession.provider !== 'mimo') throw new Error('composer fallback must use live default mimo, got ' + liveSession.provider);
  if (syncedTo !== 'mimo') throw new Error('composer sync must target live default mimo, got ' + syncedTo);
  // A permitted continuation retains its exact provider.
  liveSession.provider = 'mimo';
  runBodies.length = 0;
  await runtime.Composer.continueTask('s1');
  const continued = runBodies.find((r) => r.url === '/api/run' && r.body && r.body.input?.kind === 'resume_run');
  if (!continued) throw new Error('continueTask did not POST /api/run with input.kind=resume_run');
  if (continued.body.input.source_run_id !== 'source-run-1') throw new Error('continueTask lost source run id');
  if (continued.body.provider !== 'mimo') throw new Error('continueTask must preserve the stored provider, got ' + continued.body.provider);
  // A newer approval continuation owns the subsequent Continue action.
  liveSession.messages.push({type:'shell_approval_status', approval:{
    continuation:'started', continuation_run_id:'newer-approval-run'}});
  runBodies.length = 0;
  await runtime.Composer.continueTask('s1');
  if (runBodies[0].body.input.source_run_id !== 'newer-approval-run') throw new Error('newer approval run was lost');
  // Removed connections keep their identity and cannot submit or substitute.
  liveSession.provider = 'removed-id';
  runBodies.length = 0;
  await runtime.Composer.continueTask('s1');
  if (runBodies.length) throw new Error('removed connection submitted a continuation');
  if (liveSession.provider !== 'removed-id') throw new Error('removed connection identity changed');
  console.log('EXECUTABLE_SHRINK_LIVE_OK');
}
main().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""
        harness = harness.replace("runtime.", "window.")
        # P1 harness completeness locks (fail red until the fake DOM is fixed).
        self.assertIn("local-config-close", harness)
        self.assertIn("local-config-save", harness)
        self.assertIn("local-config-pop", harness)
        # Fake menu must truly record/delete buttons.
        self.assertIn("menuButtons", harness)
        self.assertIn("menuButtons.slice()", harness)
        self.assertIn("menuButtons.push(btn)", harness)
        self.assertIn("menuButtons.splice", harness)
        # Menu shrink must be asserted by button count, not just state array.
        self.assertIn("provider-item", harness)
        self.assertIn("menuButtonCount", harness)
        # Default flip must go through applyConfig, not direct setProviders.
        self.assertIn("applyConfig({", harness)
        self.assertIn("default: 'mimo'", harness)
        # continueTask removed-provider body must be covered.
        self.assertIn("continueTask", harness)
        self.assertIn("/api/run", harness)
        self.assertIn("input?.kind === 'resume_run'", harness)
        if shutil.which("node") is None:
            self.skipTest("node unavailable for executable JS behavior check")
        # Compile the real TypeScript modules used by this scenario, then run
        # the existing DOM harness against those generated modules.
        compiler = _esbuild_command(ROOT)
        if compiler is None:
            self.skipTest("esbuild unavailable")
        with tempfile.TemporaryDirectory() as build_dir:
            assets = Path(build_dir) / "assets"
            assets.mkdir()
            for name in ("models", "ui_state", "provider_ui", "composer"):
                source = ROOT / "frontend/src" / f"{name}.ts"
                output = assets / f"{name}.js"
                completed = subprocess.run(
                        [compiler, str(source),
                         "--bundle", "--format=iife", f"--outfile={output}"],
                    capture_output=True, text=True, encoding="utf-8", timeout=30,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
                tmp.write(harness)
                harness_path = tmp.name
            try:
                completed = subprocess.run(
                    ["node", harness_path, build_dir],
                    capture_output=True, text=True, encoding="utf-8", timeout=30,
                )
            finally:
                Path(harness_path).unlink(missing_ok=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("EXECUTABLE_SHRINK_LIVE_OK", completed.stdout)


if __name__ == "__main__":
    unittest.main()
