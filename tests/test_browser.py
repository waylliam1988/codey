from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from codey import browser


class BrowserProviderWrapperTests(unittest.TestCase):
    def test_deepseek_wrapper_uses_generic_chat_page(self) -> None:
        session = object()
        profile = Path("deepseek-profile")
        with mock.patch.object(browser, "open_chat_page", return_value=session) as opened:
            result = browser.open_deepseek(port=9333, profile=profile)

        self.assertIs(result, session)
        opened.assert_called_once_with(
            browser.DEEPSEEK_URL,
            "chat.deepseek.com",
            port=9333,
            profile=profile,
        )

    def test_qwen_wrapper_uses_generic_chat_page(self) -> None:
        session = object()
        profile = Path("qwen-profile")
        with mock.patch.object(browser, "open_chat_page", return_value=session) as opened:
            result = browser.open_qwen(port=9444, profile=profile)

        self.assertIs(result, session)
        opened.assert_called_once_with(
            browser.QWEN_URL,
            "chat.qwen.ai",
            port=9444,
            profile=profile,
        )


if __name__ == "__main__":
    unittest.main()
