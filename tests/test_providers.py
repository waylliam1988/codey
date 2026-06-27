from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.providers import DeepSeekWebProvider, QwenWebProvider
from codey.providers import deepseek_web, qwen_web, registry


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
        session = SimpleNamespace(page=page, close=mock.Mock())
        provider = DeepSeekWebProvider(session)

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
        session.close.assert_called_once_with()


class QwenWebProviderTests(unittest.TestCase):
    def test_connect_wraps_browser_session(self) -> None:
        session = SimpleNamespace()
        profile = Path("qwen-profile")
        with mock.patch.object(qwen_web, "open_qwen", return_value=session) as opened:
            provider = QwenWebProvider.connect(port=9444, profile=profile)

        self.assertIs(provider.session, session)
        opened.assert_called_once_with(port=9444, profile=profile)

    def test_delegates_chat_operations_and_closes_playwright(self) -> None:
        page = SimpleNamespace(url="https://chat.qwen.ai/c/test")
        session = SimpleNamespace(page=page, close=mock.Mock())
        provider = QwenWebProvider(session)

        with (
            mock.patch.object(qwen_web.qwen, "new_chat") as new_chat,
            mock.patch.object(qwen_web.qwen, "chat", return_value="qwen reply") as chat,
        ):
            provider.new_chat()
            reply = provider.send("hello", timeout=15.0)

        self.assertEqual(provider.name, "Qwen Studio")
        self.assertEqual(provider.location, "https://chat.qwen.ai/c/test")
        self.assertEqual(reply, "qwen reply")
        new_chat.assert_called_once_with(page)
        chat.assert_called_once_with(page, "hello", response_timeout=15.0)

        provider.close()
        session.close.assert_called_once_with()


class ProviderRegistryTests(unittest.TestCase):
    def test_connect_provider_dispatches_supported_ids(self) -> None:
        deepseek = object()
        qwen = object()
        with (
            mock.patch.object(registry.DeepSeekWebProvider, "connect", return_value=deepseek),
            mock.patch.object(registry.QwenWebProvider, "connect", return_value=qwen),
        ):
            self.assertIs(registry.connect_provider("deepseek", port=9222), deepseek)
            self.assertIs(registry.connect_provider("qwen", port=9222), qwen)

    def test_connect_provider_rejects_unknown_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported provider"):
            registry.connect_provider("unknown")


if __name__ == "__main__":
    unittest.main()
