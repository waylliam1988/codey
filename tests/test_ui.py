from __future__ import annotations

import unittest
from pathlib import Path


HTML = (Path(__file__).resolve().parents[1] / "codey" / "web" / "index.html").read_text(encoding="utf-8")


class ProviderSelectorUiTests(unittest.TestCase):
    def test_provider_selector_lists_deepseek_and_qwen(self) -> None:
        self.assertIn('id="provider-select"', HTML)
        self.assertIn('<option value="deepseek">DeepSeek</option>', HTML)
        self.assertIn('<option value="qwen">Qwen</option>', HTML)

    def test_run_and_continue_requests_keep_session_provider(self) -> None:
        self.assertIn("provider: currentProviderId()", HTML)
        self.assertIn("provider: s.provider || DEFAULT_PROVIDER", HTML)
        self.assertIn("provider: ['deepseek', 'qwen'].includes(s.provider)", HTML)

    def test_deleting_last_session_preserves_selected_provider(self) -> None:
        self.assertIn("const fallbackProvider = currentProviderId()", HTML)
        self.assertIn("provider: fallbackProvider", HTML)

    def test_task_strip_renders_status_and_actions(self) -> None:
        self.assertIn("function renderTaskStrip(", HTML)
        self.assertIn('id="task-strip"', HTML)
        self.assertIn('class="status-card"', HTML)
        self.assertIn("重试上一任务", HTML)
        self.assertIn("继续此任务", HTML)
        self.assertIn("task-strip", HTML)


if __name__ == "__main__":
    unittest.main()
