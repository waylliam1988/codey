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
        self.assertIn('id="provider-dot"', HTML)
        self.assertIn("--ok-dot:", HTML)
        self.assertIn(".dot.ok", HTML)
        self.assertIn(".provider-item.active .check", HTML)

    def test_provider_selector_orders_deepseek_mimo_qwen(self) -> None:
        deepseek = HTML.index('data-provider="deepseek"')
        mimo = HTML.index('data-provider="mimo"')
        qwen = HTML.index('data-provider="qwen"')

        self.assertLess(deepseek, mimo)
        self.assertLess(mimo, qwen)

    def test_run_and_continue_requests_keep_session_provider(self) -> None:
        self.assertIn("provider: currentProviderId()", HTML)
        self.assertIn("provider: s.provider || DEFAULT_PROVIDER", HTML)
        self.assertIn("provider: PROVIDERS.includes(s.provider)", HTML)

    def test_retry_uses_current_session_model_picker(self) -> None:
        retry_start = HTML.index("function retryTask(sessionId)")
        retry_end = HTML.index("function sessionProjectPath", retry_start)
        retry_block = HTML[retry_start:retry_end]
        self.assertIn("syncProviderUI(s.provider || DEFAULT_PROVIDER)", retry_block)
        self.assertIn("$('send').click()", retry_block)
        self.assertIn("provider: currentProviderId()", HTML)

    def test_provider_selector_is_enabled_when_idle(self) -> None:
        self.assertIn("$('provider-button').disabled = running", HTML)
        self.assertIn("$('provider-button').disabled = false", HTML)

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

    def test_send_failures_render_inline_error(self) -> None:
        self.assertIn("function addSendError(sessionId)", HTML)
        self.assertIn("Could not send the message", HTML)
        self.assertIn("if (r.status === 409) { addSendError(activeId); return; }", HTML)
        self.assertIn("if (r.status === 409 || !r.ok) addSendError(sessionId);", HTML)
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

    def test_review_status_is_quiet_and_has_no_switch(self) -> None:
        self.assertIn("data.type === 'review'", HTML)
        self.assertIn("{ type: 'review', text: data.text }", HTML)
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
