from __future__ import annotations

import unittest
from pathlib import Path


HTML = (Path(__file__).resolve().parents[1] / "codey" / "web" / "index.html").read_text(encoding="utf-8")


class ProviderSelectorUiTests(unittest.TestCase):
    def test_provider_selector_lists_deepseek_and_qwen(self) -> None:
        self.assertIn('id="provider-button"', HTML)
        self.assertIn('id="provider-menu"', HTML)
        self.assertIn('data-provider="deepseek"', HTML)
        self.assertIn('data-provider="qwen"', HTML)
        self.assertIn('id="provider-dot"', HTML)
        self.assertIn('background: var(--ok);', HTML)
        self.assertIn(".provider-item.active .dot", HTML)

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

    def test_composer_holds_model_picker_with_green_dot(self) -> None:
        self.assertIn('class="provider-chooser"', HTML)
        self.assertIn('class="provider-button"', HTML)
        self.assertIn('class="provider-menu"', HTML)
        self.assertIn('class="provider-item"', HTML)

    def test_welcome_keeps_status_copy_without_example_cards(self) -> None:
        self.assertIn("嗨，今天想做点什么？", HTML)
        self.assertIn("当前是纯聊天。选择或新建项目后", HTML)
        self.assertIn("Codey 会在这个目录里读写文件", HTML)
        self.assertNotIn("打个招呼", HTML)
        self.assertNotIn("生成 snake.py", HTML)
        self.assertNotIn("写 README", HTML)
        self.assertNotIn("找 bug", HTML)


if __name__ == "__main__":
    unittest.main()
