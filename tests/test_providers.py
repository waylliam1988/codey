from __future__ import annotations

import unittest
import tempfile
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey import cancellation
from codey.adapter_overrides import AdapterOverride
from codey.provider_diagnostics import ProviderActionError
from codey.providers import (
    DeepSeekWebProvider,
    GlmWebProvider,
    MimoWebProvider,
    QwenWebProvider,
)
from codey.providers import deepseek_web, glm_web, local_openai, mimo_web, qwen_web, registry


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
            isolated=False,
            fresh_tab=False,
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
            with self.assertRaises(ProviderActionError) as raised:
                provider.send("hello")

        self.assertIsInstance(raised.exception.__cause__, TimeoutError)
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
            isolated=False,
            fresh_tab=False,
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

    def test_new_chat_forwards_optional_recovery_budget(self) -> None:
        page = SimpleNamespace(url="https://chat.qwen.ai/")
        page.title = mock.Mock(return_value="Qwen")
        provider = QwenWebProvider(SimpleNamespace(page=page, close=mock.Mock()))

        with mock.patch.object(qwen_web.qwen, "new_chat") as new_chat:
            provider.new_chat(timeout=7.5)

        new_chat.assert_called_once_with(page, timeout=7.5)


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
            isolated=False,
            fresh_tab=False,
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


class GlmWebProviderTests(unittest.TestCase):
    def test_connect_wraps_browser_session(self) -> None:
        session = SimpleNamespace()
        profile = Path("glm-profile")
        with mock.patch.object(glm_web, "open_glm", return_value=session) as opened:
            provider = GlmWebProvider.connect(port=9666, profile=profile)

        self.assertIs(provider.session, session)
        opened.assert_called_once_with(
            port=9666,
            profile=profile,
            open_if_missing=True,
            bring_to_front=True,
            isolated=False,
            fresh_tab=False,
        )

    def test_delegates_operations_while_driver_owns_formatting_hint(self) -> None:
        page = SimpleNamespace(url="https://chatglm.cn/main/alltoolsdetail?lang=zh")
        page.title = mock.Mock(return_value="智谱清言")
        session = SimpleNamespace(page=page, close=mock.Mock())
        provider = GlmWebProvider(session)

        with (
            mock.patch.object(glm_web.glm, "new_chat") as new_chat,
            mock.patch.object(glm_web.glm, "chat", return_value="glm reply") as chat,
        ):
            provider.new_chat()
            reply = provider.send("hello", timeout=25.0)

        self.assertEqual(provider.name, "GLM")
        self.assertEqual(reply, "glm reply")
        new_chat.assert_called_once_with(page)
        chat.assert_called_once_with(page, "hello", response_timeout=25.0)

        provider.close()
        session.close.assert_called_once_with()

    def test_blank_message_is_rejected_before_provider_touches_page(self) -> None:
        page = mock.Mock(url="https://chatglm.cn/main/alltoolsdetail?lang=zh")
        page.title.return_value = "智谱清言"
        provider = GlmWebProvider(SimpleNamespace(page=page, close=mock.Mock()))

        with self.assertRaisesRegex(ValueError, "cannot be blank"):
            provider.send("   ")

        page.locator.assert_not_called()
        page.title.assert_not_called()


class ProviderTimeoutBoundaryTests(unittest.TestCase):
    def test_explicit_send_timeout_bounds_entire_provider_call(self) -> None:
        cases = (
            (DeepSeekWebProvider, deepseek_web.deepseek),
            (QwenWebProvider, qwen_web.qwen),
            (MimoWebProvider, mimo_web.mimo),
            (GlmWebProvider, glm_web.glm),
        )
        for provider_type, driver in cases:
            with self.subTest(provider=provider_type.__name__):
                page = SimpleNamespace(url="https://chat.example/")
                page.title = mock.Mock(return_value="Chat")
                provider = provider_type(
                    SimpleNamespace(page=page, close=mock.Mock())
                )

                def wait_past_deadline(*_args, **_kwargs):
                    cancellation.wait(60)

                with mock.patch.object(driver, "chat", side_effect=wait_past_deadline) as chat:
                    with self.assertRaises(ProviderActionError) as raised:
                        provider.send("hello", timeout=0)
                    self.assertIsInstance(
                        raised.exception.__cause__,
                        cancellation.DeadlineExceeded,
                    )

                chat.assert_called_once()


class ProviderRegistryTests(unittest.TestCase):
    def test_provider_registry_surfaces_stay_in_sync(self) -> None:
        ids = set(registry.provider_ids())

        self.assertEqual(ids, set(registry.PROVIDER_TYPES))
        self.assertEqual(set(registry.WEB_PROVIDER_LABELS), set(registry.PROVIDER_URL_CONTAINS))
        self.assertIn("local", ids)
        self.assertNotIn("local", registry.PROVIDER_URL_CONTAINS)

    def test_provider_ids_are_ordered_for_ui(self) -> None:
        self.assertEqual(registry.provider_ids(), ("deepseek", "mimo", "qwen", "glm", "local"))

    def test_provider_tab_availability_returns_all_registered_providers(self) -> None:
        with mock.patch.object(
            registry,
            "detect_open_provider_tabs",
            return_value={"deepseek": True, "mimo": True},
        ), mock.patch.object(registry, "local_endpoint_available", return_value=True):
            statuses = registry.provider_tab_availability()

        self.assertEqual(
            statuses,
            {"deepseek": True, "mimo": True, "qwen": False, "glm": False, "local": True},
        )

    def test_connect_provider_dispatches_supported_ids(self) -> None:
        deepseek = object()
        qwen = object()
        mimo = object()
        glm = object()
        local = object()
        with (
            mock.patch.object(registry, "load_enabled_override", return_value=None),
            mock.patch.object(registry.DeepSeekWebProvider, "connect", return_value=deepseek),
            mock.patch.object(registry.QwenWebProvider, "connect", return_value=qwen),
            mock.patch.object(registry.MimoWebProvider, "connect", return_value=mimo),
            mock.patch.object(registry.GlmWebProvider, "connect", return_value=glm),
            mock.patch.object(registry.LocalOpenAIProvider, "connect", return_value=local),
        ):
            self.assertIs(registry.connect_provider("deepseek", port=9222), deepseek)
            self.assertIs(registry.connect_provider("qwen", port=9222), qwen)
            self.assertIs(registry.connect_provider("mimo", port=9222), mimo)
            self.assertIs(registry.connect_provider("glm", port=9222), glm)
            self.assertIs(registry.connect_provider("local", port=9222), local)

    def test_local_provider_uses_first_available_default_endpoint(self) -> None:
        seen: list[str] = []

        def probe(url: str, **_kwargs):
            seen.append(url)
            if url == "http://127.0.0.1:11434/v1":
                return local_openai.LocalEndpoint(url, ("llama",))
            return None

        with (
            mock.patch.dict(
                "os.environ",
                {
                    local_openai.LOCAL_BASE_URL_ENV: "",
                    local_openai.LOCAL_MODEL_ENV: "",
                    local_openai.LOCAL_API_KEY_ENV: "",
                },
                clear=False,
            ),
            mock.patch.object(local_openai, "load_local_config", return_value={}),
            mock.patch.object(local_openai, "probe_local_endpoint", side_effect=probe),
        ):
            provider = local_openai.LocalOpenAIProvider()

        self.assertEqual(provider.base_url, "http://127.0.0.1:11434/v1")
        self.assertEqual(provider.model, "local-model")
        self.assertEqual(
            seen,
            ["http://127.0.0.1:1234/v1", "http://127.0.0.1:11434/v1"],
        )

    def test_local_connect_prefers_remembered_config(self) -> None:
        endpoint = local_openai.LocalEndpoint("http://127.0.0.1:5001/v1", ("gemma",))
        with (
            mock.patch.object(
                local_openai,
                "load_local_config",
                return_value={"base_url": endpoint.base_url, "model": "chosen", "api_key": "secret"},
            ),
            mock.patch.object(local_openai, "probe_local_endpoint", return_value=endpoint),
        ):
            provider = local_openai.LocalOpenAIProvider.connect()

        self.assertEqual(provider.base_url, endpoint.base_url)
        self.assertEqual(provider.model, "chosen")
        self.assertEqual(provider.api_key, "secret")

    def test_local_probe_sends_authorization_when_api_key_is_configured(self) -> None:
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return b'{"data":[{"id":"llama"}]}'

        def urlopen(request: urllib.request.Request, timeout: float):
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return Response()

        with mock.patch.object(local_openai.urllib.request, "urlopen", side_effect=urlopen):
            endpoint = local_openai.probe_local_endpoint(
                "http://127.0.0.1:1234/v1",
                api_key="secret",
                timeout=3,
            )

        self.assertIsNotNone(endpoint)
        self.assertEqual(captured["authorization"], "Bearer secret")
        self.assertEqual(captured["timeout"], 3)

    def test_local_config_preserves_api_key_when_new_key_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "local-openai.json"
            with mock.patch.object(local_openai, "_config_path", return_value=path):
                local_openai.save_local_config("http://127.0.0.1:1234/v1", "old", "secret")
                local_openai.save_local_config("http://127.0.0.1:1234/v1", "new", None)
                preserved = local_openai.load_local_config()
                local_openai.save_local_config(
                    "http://127.0.0.1:1234/v1",
                    "new",
                    None,
                    clear_api_key=True,
                )
                cleared = local_openai.load_local_config()

        self.assertEqual(preserved["api_key"], "secret")
        self.assertEqual(preserved["model"], "new")
        self.assertEqual(cleared["api_key"], "")

    def test_connect_existing_provider_does_not_open_or_raise_window(self) -> None:
        qwen = object()
        with (
            mock.patch.object(registry, "load_enabled_override", return_value=None),
            mock.patch.object(registry.QwenWebProvider, "connect", return_value=qwen) as connected,
        ):
            self.assertIs(registry.connect_existing_provider("qwen"), qwen)

        connected.assert_called_once_with(
            port=registry.DEFAULT_PORT,
            profile=registry.DEFAULT_PROFILE,
            open_if_missing=False,
            bring_to_front=False,
        )

    def test_connect_fresh_provider_tab_opens_isolated_background_tab(self) -> None:
        qwen = object()
        with (
            mock.patch.object(registry, "load_enabled_override", return_value=None),
            mock.patch.object(registry.QwenWebProvider, "connect", return_value=qwen) as connected,
        ):
            self.assertIs(registry.connect_fresh_provider_tab("qwen"), qwen)

        connected.assert_called_once_with(
            port=registry.DEFAULT_PORT,
            profile=registry.DEFAULT_PROFILE,
            open_if_missing=True,
            bring_to_front=False,
            fresh_tab=True,
        )

    def test_connect_fresh_provider_tab_uses_enabled_adapter_override_worker(self) -> None:
        override = AdapterOverride(
            "qwen",
            7,
            "active",
            Path("override-root"),
        )
        with (
            mock.patch.object(registry, "load_enabled_override", return_value=override),
            mock.patch.object(registry, "WorkerChatProvider", return_value="worker") as worker,
            mock.patch.object(registry.QwenWebProvider, "connect") as connected,
        ):
            self.assertEqual(registry.connect_fresh_provider_tab("qwen"), "worker")

        worker.assert_called_once_with(
            "qwen",
            override,
            port=registry.DEFAULT_PORT + registry.PROVIDER_WORKER_PORT_OFFSETS["qwen"],
        )
        connected.assert_not_called()

    def test_borrow_open_provider_reuses_sibling_page_without_closing_context(self) -> None:
        owner = SimpleNamespace(url="https://chat.deepseek.com/")
        sibling = SimpleNamespace(url="https://chat.qwen.ai/c/1")
        owner.context = SimpleNamespace(pages=[owner, sibling])

        provider = registry.borrow_open_provider("qwen", owner)

        self.assertIsInstance(provider, QwenWebProvider)
        self.assertIs(provider.session.page, sibling)
        provider.close()

    def test_borrow_open_glm_provider_reuses_sibling_page(self) -> None:
        owner = SimpleNamespace(url="https://chat.deepseek.com/")
        sibling = SimpleNamespace(url="https://chatglm.cn/main/alltoolsdetail?lang=zh")
        owner.context = SimpleNamespace(pages=[owner, sibling])

        provider = registry.borrow_open_provider("glm", owner)

        self.assertIsInstance(provider, GlmWebProvider)
        self.assertIs(provider.session.page, sibling)

    def test_borrow_open_provider_does_not_open_missing_tab(self) -> None:
        owner = SimpleNamespace(url="https://chat.deepseek.com/")
        owner.context = SimpleNamespace(pages=[owner])

        self.assertIsNone(registry.borrow_open_provider("qwen", owner))

    def test_connect_provider_rejects_unknown_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported provider"):
            registry.connect_provider("unknown")


if __name__ == "__main__":
    unittest.main()
