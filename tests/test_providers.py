from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.providers import DeepSeekWebProvider, MimoWebProvider, QwenWebProvider
from codey.providers import deepseek_web, mimo_web, qwen_web, registry


class DeepSeekWebProviderTests(unittest.TestCase):
    def test_connect_wraps_browser_session(self) -> None:
        session = SimpleNamespace()
        profile = Path("profile")
        with mock.patch.object(deepseek_web, "open_deepseek", return_value=session) as opened:
            provider = DeepSeekWebProvider.connect(port=9333, profile=profile)

        self.assertIs(provider.session, session)
        opened.assert_called_once_with(
            port=9333,
            profile=profile,
            open_if_missing=True,
            bring_to_front=True,
        )

    def test_delegates_chat_operations_and_closes_playwright(self) -> None:
        page = SimpleNamespace(url="https://chat.deepseek.com/")
        page.title = mock.Mock(return_value="DeepSeek")
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

    def test_send_failure_records_small_diagnostic(self) -> None:
        page = SimpleNamespace(url="https://chat.deepseek.com/c/1")
        page.title = mock.Mock(return_value="DeepSeek")
        session = SimpleNamespace(page=page, close=mock.Mock())
        provider = DeepSeekWebProvider(session)

        with mock.patch.object(
            deepseek_web.deepseek,
            "chat",
            side_effect=TimeoutError("response timed out"),
        ):
            with self.assertRaisesRegex(TimeoutError, "response timed out"):
                provider.send("hello")

        self.assertIsNotNone(provider.last_failure)
        self.assertEqual(provider.last_failure.model, "DeepSeek Web")
        self.assertEqual(provider.last_failure.action, "send")
        self.assertEqual(provider.last_failure.url, "https://chat.deepseek.com/c/1")
        self.assertEqual(provider.last_failure.title, "DeepSeek")
        self.assertEqual(provider.last_failure.message, "response timed out")


class QwenWebProviderTests(unittest.TestCase):
    def test_connect_wraps_browser_session(self) -> None:
        session = SimpleNamespace()
        profile = Path("qwen-profile")
        with mock.patch.object(qwen_web, "open_qwen", return_value=session) as opened:
            provider = QwenWebProvider.connect(port=9444, profile=profile)

        self.assertIs(provider.session, session)
        opened.assert_called_once_with(
            port=9444,
            profile=profile,
            open_if_missing=True,
            bring_to_front=True,
        )

    def test_delegates_chat_operations_and_closes_playwright(self) -> None:
        page = SimpleNamespace(url="https://chat.qwen.ai/c/test")
        page.title = mock.Mock(return_value="Qwen")
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


class MimoWebProviderTests(unittest.TestCase):
    def test_connect_wraps_browser_session(self) -> None:
        session = SimpleNamespace()
        profile = Path("mimo-profile")
        with mock.patch.object(mimo_web, "open_mimo", return_value=session) as opened:
            provider = MimoWebProvider.connect(port=9555, profile=profile)

        self.assertIs(provider.session, session)
        opened.assert_called_once_with(
            port=9555,
            profile=profile,
            open_if_missing=True,
            bring_to_front=True,
        )

    def test_delegates_chat_operations_and_closes_playwright(self) -> None:
        page = SimpleNamespace(url="https://aistudio.xiaomimimo.com/#/c")
        page.title = mock.Mock(return_value="MiMo")
        session = SimpleNamespace(page=page, close=mock.Mock())
        provider = MimoWebProvider(session)

        with (
            mock.patch.object(mimo_web.mimo, "new_chat") as new_chat,
            mock.patch.object(mimo_web.mimo, "chat", return_value="mimo reply") as chat,
        ):
            provider.new_chat()
            reply = provider.send("hello", timeout=20.0)

        self.assertEqual(provider.name, "Xiaomi MiMo Chat")
        self.assertEqual(provider.location, "https://aistudio.xiaomimimo.com/#/c")
        self.assertEqual(reply, "mimo reply")
        new_chat.assert_called_once_with(page)
        chat.assert_called_once_with(page, "hello", response_timeout=20.0)

        provider.close()
        session.close.assert_called_once_with()


class ProviderRegistryTests(unittest.TestCase):
    def test_provider_ids_are_ordered_for_ui(self) -> None:
        self.assertEqual(registry.provider_ids(), ("deepseek", "mimo", "qwen"))

    def test_provider_tab_availability_returns_all_registered_providers(self) -> None:
        with mock.patch.object(
            registry,
            "detect_open_provider_tabs",
            return_value={"deepseek": True, "mimo": True},
        ):
            statuses = registry.provider_tab_availability()

        self.assertEqual(statuses, {"deepseek": True, "mimo": True, "qwen": False})

    def test_connect_provider_dispatches_supported_ids(self) -> None:
        deepseek = object()
        qwen = object()
        mimo = object()
        with (
            mock.patch.object(registry.DeepSeekWebProvider, "connect", return_value=deepseek),
            mock.patch.object(registry.QwenWebProvider, "connect", return_value=qwen),
            mock.patch.object(registry.MimoWebProvider, "connect", return_value=mimo),
        ):
            self.assertIs(registry.connect_provider("deepseek", port=9222), deepseek)
            self.assertIs(registry.connect_provider("qwen", port=9222), qwen)
            self.assertIs(registry.connect_provider("mimo", port=9222), mimo)

    def test_connect_existing_provider_does_not_open_or_raise_window(self) -> None:
        qwen = object()
        with mock.patch.object(registry.QwenWebProvider, "connect", return_value=qwen) as connected:
            self.assertIs(registry.connect_existing_provider("qwen"), qwen)

        connected.assert_called_once_with(
            port=registry.DEFAULT_PORT,
            profile=registry.DEFAULT_PROFILE,
            open_if_missing=False,
            bring_to_front=False,
        )

    def test_borrow_open_provider_reuses_sibling_page_without_closing_context(self) -> None:
        owner = SimpleNamespace(url="https://chat.deepseek.com/")
        sibling = SimpleNamespace(url="https://chat.qwen.ai/c/1")
        owner.context = SimpleNamespace(pages=[owner, sibling])

        provider = registry.borrow_open_provider("qwen", owner)

        self.assertIsInstance(provider, QwenWebProvider)
        self.assertIs(provider.session.page, sibling)
        provider.close()

    def test_borrow_open_provider_does_not_open_missing_tab(self) -> None:
        owner = SimpleNamespace(url="https://chat.deepseek.com/")
        owner.context = SimpleNamespace(pages=[owner])

        self.assertIsNone(registry.borrow_open_provider("qwen", owner))

    def test_connect_provider_rejects_unknown_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported provider"):
            registry.connect_provider("unknown")


if __name__ == "__main__":
    unittest.main()
