from __future__ import annotations

import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey.providers import (
    DeepSeekWebProvider,
    GlmWebProvider,
    MimoWebProvider,
    QwenWebProvider,
    StepFunWebProvider,
    local_config,
    local_discovery,
    local_openai,
    registry,
    web_driver,
    web_provider,
)
from codey.providers.diagnostics import FAILURE_RESPONSE_MISSING, ProviderActionError
from codey.providers.web_drivers import deepseek, glm, mimo, qwen, stepfun
from codey.repairs.adapter_overrides import AdapterOverride
from codey.runtime.core import cancellation


class DeepSeekWebProviderTests(unittest.TestCase):
    def test_connect_wraps_browser_session(self) -> None:
        session = SimpleNamespace()
        profile = Path("profile")
        with mock.patch.object(web_provider.browser, "open_deepseek", return_value=session) as opened:
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
            mock.patch.object(deepseek, "new_chat") as new_chat,
            mock.patch.object(deepseek, "chat", return_value="reply") as chat,
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
            deepseek,
            "chat",
            side_effect=TimeoutError("response timed out"),
        ), self.assertRaises(ProviderActionError) as raised:
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
        with mock.patch.object(web_provider.browser, "open_qwen", return_value=session) as opened:
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
            mock.patch.object(qwen, "new_chat") as new_chat,
            mock.patch.object(qwen, "chat", return_value="qwen reply") as chat,
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

        with mock.patch.object(qwen, "new_chat") as new_chat:
            provider.new_chat(timeout=7.5)

        new_chat.assert_called_once_with(page, timeout=7.5)


class StepFunWebProviderTests(unittest.TestCase):
    def test_connect_wraps_browser_session(self) -> None:
        session = SimpleNamespace()
        profile = Path("stepfun-profile")
        with mock.patch.object(web_provider.browser, "open_stepfun", return_value=session) as opened:
            provider = StepFunWebProvider.connect(port=9555, profile=profile)

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
        page = SimpleNamespace(url="https://chat.stepfun.com/chats/")
        page.title = mock.Mock(return_value="StepFun")
        session = SimpleNamespace(page=page, close=mock.Mock())
        provider = StepFunWebProvider(session)

        with (
            mock.patch.object(stepfun, "new_chat") as new_chat,
            mock.patch.object(stepfun, "chat", return_value="stepfun reply") as chat,
        ):
            provider.new_chat()
            reply = provider.send("hello", timeout=20.0)

        self.assertEqual(provider.name, "StepFun Chat")
        self.assertEqual(provider.location, "https://chat.stepfun.com/chats/")
        self.assertEqual(reply, "stepfun reply")
        new_chat.assert_called_once_with(page)
        chat.assert_called_once_with(page, "hello", response_timeout=20.0)

        provider.close()
        session.close.assert_called_once_with()


class MimoWebProviderTests(unittest.TestCase):
    def test_connect_wraps_browser_session(self) -> None:
        session = SimpleNamespace()
        profile = Path("mimo-profile")
        with mock.patch.object(web_provider.browser, "open_mimo", return_value=session) as opened:
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
            mock.patch.object(mimo, "new_chat") as new_chat,
            mock.patch.object(mimo, "chat", return_value="mimo reply") as chat,
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
        with mock.patch.object(web_provider.browser, "open_glm", return_value=session) as opened:
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
        page = SimpleNamespace(url="https://chatglm.cn/")
        page.title = mock.Mock(return_value="智谱清言")
        session = SimpleNamespace(page=page, close=mock.Mock())
        provider = GlmWebProvider(session)

        with (
            mock.patch.object(glm, "new_chat") as new_chat,
            mock.patch.object(glm, "chat", return_value="glm reply") as chat,
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
        page = mock.Mock(url="https://chatglm.cn/")
        page.title.return_value = "智谱清言"
        provider = GlmWebProvider(SimpleNamespace(page=page, close=mock.Mock()))

        with self.assertRaisesRegex(ValueError, "cannot be blank"):
            provider.send("   ")

        page.locator.assert_not_called()
        page.title.assert_not_called()


class ProviderTimeoutBoundaryTests(unittest.TestCase):
    def test_explicit_send_timeout_bounds_entire_provider_call(self) -> None:
        cases = (
            (DeepSeekWebProvider, deepseek, "TIMEOUT_GRACE"),
            (QwenWebProvider, qwen, "TIMEOUT_GRACE"),
            (MimoWebProvider, mimo, "TIMEOUT_GRACE"),
            (StepFunWebProvider, stepfun, "TIMEOUT_GRACE"),
            (GlmWebProvider, glm, "RESPONSE_TIMEOUT_GRACE"),
        )
        for provider_type, driver, grace_name in cases:
            with self.subTest(provider=provider_type.__name__):
                page = SimpleNamespace(url="https://chat.example/")
                page.title = mock.Mock(return_value="Chat")
                provider = provider_type(
                    SimpleNamespace(page=page, close=mock.Mock())
                )

                def wait_past_deadline(*_args, **_kwargs):
                    cancellation.wait(60)

                # Shrink the grace so the test proves the budget math
                # (response_timeout + grace + margin bounds the call) without
                # actually waiting seconds.
                with (
                    mock.patch.object(driver, grace_name, 0.4),
                    mock.patch.object(web_driver, "WEB_DEADLINE_MARGIN_SECONDS", 0.1),
                    mock.patch.object(driver, "chat", side_effect=wait_past_deadline) as chat,
                ):
                    with self.assertRaises(ProviderActionError) as raised:
                        provider.send("hello", timeout=0)
                    failure = raised.exception.failure
                    self.assertEqual(failure.kind, FAILURE_RESPONSE_MISSING)
                    # Standard capture diagnostics: completion stage plus
                    # the page url/title context of the dead wait.
                    self.assertEqual(failure.stage, "completion")
                    self.assertEqual(failure.model, provider.name)
                    self.assertEqual(failure.url, "https://chat.example/")
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
        self.assertEqual(
            registry.provider_ids(),
            ("deepseek", "mimo", "stepfun", "qwen", "glm", "local"),
        )

    def test_provider_tab_availability_returns_all_registered_providers(self) -> None:
        with mock.patch.object(
            registry,
            "detect_open_provider_tabs",
            return_value={"deepseek": True, "stepfun": True},
        ), mock.patch.object(registry, "local_endpoint_available", return_value=True):
            statuses = registry.provider_tab_availability()

        self.assertEqual(
            statuses,
            {
                "deepseek": True,
                "mimo": False,
                "stepfun": True,
                "qwen": False,
                "glm": False,
                "local": True,
            },
        )

    def test_connect_provider_dispatches_supported_ids(self) -> None:
        deepseek = object()
        qwen = object()
        stepfun = object()
        glm = object()
        local = object()
        with (
            mock.patch.object(registry, "load_enabled_override", return_value=None),
            mock.patch.object(registry.DeepSeekWebProvider, "connect", return_value=deepseek),
            mock.patch.object(registry.QwenWebProvider, "connect", return_value=qwen),
            mock.patch.object(registry.StepFunWebProvider, "connect", return_value=stepfun),
            mock.patch.object(registry.GlmWebProvider, "connect", return_value=glm),
            mock.patch.object(registry.LocalOpenAIProvider, "connect", return_value=local),
        ):
            self.assertIs(registry.connect_provider("deepseek", port=9222), deepseek)
            self.assertIs(registry.connect_provider("qwen", port=9222), qwen)
            self.assertIs(registry.connect_provider("stepfun", port=9222), stepfun)
            self.assertIs(registry.connect_provider("glm", port=9222), glm)
            self.assertIs(registry.connect_provider("local", port=9222), local)

    def test_local_provider_requires_resolved_target(self) -> None:
        provider = local_openai.LocalOpenAIProvider(
            base_url="http://127.0.0.1:11434/v1", model="qwen",
        )
        self.assertEqual(provider.base_url, "http://127.0.0.1:11434/v1")
        self.assertEqual(provider.model, "qwen")
        with self.assertRaises(ValueError):
            local_openai.LocalOpenAIProvider(base_url="", model="qwen")
        with self.assertRaises(ValueError):
            local_openai.LocalOpenAIProvider(base_url="http://127.0.0.1:11434/v1", model="")

    def test_local_connect_prefers_remembered_config(self) -> None:
        endpoint = local_discovery.LocalEndpoint("http://127.0.0.1:5001/v1", ("chosen", "gemma"))
        config = local_config.LocalProviderConfig(
            base_url=endpoint.base_url,
            model="chosen",
            api_key="secret",
        )
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "LOCAL_OPENAI_BASE_URL": "",
                    "LOCAL_OPENAI_MODEL": "",
                    "LOCAL_OPENAI_API_KEY": "",
                },
                clear=False,
            ),
            mock.patch.object(
                local_config,
                "load_local_config",
                return_value=config,
            ),
            mock.patch.object(local_discovery, "resolve_local_endpoint", return_value=endpoint) as resolve,
        ):
            provider = local_openai.LocalOpenAIProvider.connect()

        self.assertEqual(provider.base_url, endpoint.base_url)
        self.assertEqual(provider.model, "chosen")
        self.assertEqual(provider.api_key, "secret")
        resolve.assert_called_once_with(base_url=endpoint.base_url, model="chosen", api_key="secret")

    def test_local_probe_sends_authorization_when_api_key_is_configured(self) -> None:
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size: int | None = None) -> bytes:
                del size
                return b'{"data":[{"id":"llama"}]}'

        def urlopen(request: urllib.request.Request, timeout: float):
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return Response()

        with mock.patch.object(local_discovery.urllib.request, "urlopen", side_effect=urlopen):
            endpoint = local_discovery.probe_local_endpoint(
                "http://127.0.0.1:1234/v1",
                api_key="secret",
                timeout=3,
            )

        self.assertIsNotNone(endpoint)
        self.assertEqual(captured["authorization"], "Bearer secret")
        self.assertEqual(captured["timeout"], 3)

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
        sibling = SimpleNamespace(url="https://chatglm.cn/")
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
