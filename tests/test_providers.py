from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.providers import DeepSeekWebProvider
from codey.providers import deepseek_web


class DeepSeekWebProviderTests(unittest.TestCase):
    def test_connect_wraps_browser_session(self) -> None:
        session = SimpleNamespace()
        profile = Path("profile")
        with mock.patch.object(deepseek_web, "open_deepseek", return_value=session) as opened:
            provider = DeepSeekWebProvider.connect(port=9333, profile=profile)

        self.assertIs(provider.session, session)
        opened.assert_called_once_with(port=9333, profile=profile)

    def test_delegates_chat_operations_and_closes_playwright(self) -> None:
        page = SimpleNamespace(url="https://chat.deepseek.com/")
        pw = mock.Mock()
        provider = DeepSeekWebProvider(SimpleNamespace(page=page, pw=pw))

        with (
            mock.patch.object(deepseek_web.deepseek, "new_chat") as new_chat,
            mock.patch.object(deepseek_web.deepseek, "chat", return_value="reply") as chat,
        ):
            provider.new_chat()
            reply = provider.send("hello", timeout=12.5)

        self.assertEqual(provider.name, "DeepSeek Web")
        self.assertEqual(provider.location, "https://chat.deepseek.com/")
        self.assertEqual(reply, "reply")
        new_chat.assert_called_once_with(page)
        chat.assert_called_once_with(page, "hello", response_timeout=12.5)

        provider.close()
        pw.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
