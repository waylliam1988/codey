from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from codey.provider_diagnostics import (
    ProviderActionError,
    ProviderFailure,
    capture_provider_failure,
    run_provider_action,
)


class ProviderDiagnosticsTests(unittest.TestCase):
    def test_capture_provider_failure_keeps_only_small_page_context(self) -> None:
        page = SimpleNamespace(url="https://chat.example/c", title=lambda: "Example Chat")
        now = datetime(2026, 6, 28, 1, 2, 3, tzinfo=timezone.utc)

        failure = capture_provider_failure(
            model="MiMo",
            action="send",
            page=page,
            error=TimeoutError("response timed out"),
            now=now,
        )

        self.assertEqual(failure.to_dict(), {
            "model": "MiMo",
            "action": "send",
            "url": "https://chat.example/c",
            "title": "Example Chat",
            "message": "response timed out",
            "kind": "transient",
            "stage": "",
            "time": "2026-06-28T01:02:03+00:00",
        })

    def test_capture_provider_failure_uses_explicit_structural_kind(self) -> None:
        from codey.provider_diagnostics import ControlMissing

        failure = capture_provider_failure(
            model="Qwen",
            action="send",
            page=SimpleNamespace(url="https://chat.qwen.ai/", title=lambda: "Qwen"),
            error=ControlMissing("message box missing"),
        )

        self.assertEqual(failure.kind, "control_missing")
        self.assertEqual(failure.stage, "input")

    def test_explicit_failure_stage_is_preserved(self) -> None:
        from codey.provider_diagnostics import ResponseMissing

        failure = capture_provider_failure(
            model="Qwen",
            action="send",
            page=None,
            error=ResponseMissing("completion marker missing", stage="completion"),
        )

        self.assertEqual(failure.kind, "response_missing")
        self.assertEqual(failure.stage, "completion")

    def test_new_chat_action_uses_new_chat_stage(self) -> None:
        failure = capture_provider_failure(
            model="Qwen",
            action="new_chat",
            page=None,
            error=TimeoutError("navigation timed out"),
        )

        self.assertEqual(failure.kind, "transient")
        self.assertEqual(failure.stage, "new_chat")

    def test_capture_provider_failure_tolerates_broken_page_accessors(self) -> None:
        class BrokenPage:
            @property
            def url(self) -> str:
                raise RuntimeError("url unavailable")

            def title(self) -> str:
                raise RuntimeError("title unavailable")

        failure = capture_provider_failure(
            model="Qwen",
            action="read_response",
            page=BrokenPage(),
            error=RuntimeError("selector missing"),
        )

        self.assertEqual(failure.url, "")
        self.assertEqual(failure.title, "")
        self.assertEqual(failure.message, "selector missing")

    def test_run_provider_action_raises_typed_failure(self) -> None:
        provider = SimpleNamespace(name="Qwen", last_failure=None)

        with self.assertRaises(ProviderActionError) as raised:
            run_provider_action(
                provider,
                action="send",
                page=SimpleNamespace(url="https://example.test", title=lambda: "Chat"),
                func=lambda: (_ for _ in ()).throw(TimeoutError("late")),
            )

        self.assertIs(raised.exception.failure, provider.last_failure)
        self.assertEqual(raised.exception.failure.kind, "transient")
        self.assertIsInstance(raised.exception.__cause__, TimeoutError)

    def test_successful_provider_action_clears_stale_failure(self) -> None:
        provider = SimpleNamespace(
            name="Qwen",
            last_failure=ProviderFailure("Qwen", "send", "", "", "old", "now"),
        )

        result = run_provider_action(
            provider,
            action="send",
            page=None,
            func=lambda: "ok",
        )

        self.assertEqual(result, "ok")
        self.assertIsNone(provider.last_failure)

    def test_login_page_is_typed_before_provider_control_logic(self) -> None:
        provider = SimpleNamespace(name="GLM", last_failure=None)
        action = mock.Mock()

        with self.assertRaises(ProviderActionError) as raised:
            run_provider_action(
                provider,
                action="send",
                page=SimpleNamespace(url="https://chat.example/login"),
                func=action,
            )

        self.assertEqual(raised.exception.failure.kind, "authentication_required")
        action.assert_not_called()

    def test_visible_challenge_is_typed_before_provider_control_logic(self) -> None:
        provider = SimpleNamespace(name="MiMo", last_failure=None)
        hidden = mock.Mock()
        hidden.count.return_value = 0
        challenge = mock.Mock()
        challenge.count.return_value = 1
        challenge.first.is_visible.return_value = True
        page = SimpleNamespace(
            url="https://chat.example/",
            locator=lambda selector: challenge if "captcha" in selector else hidden,
        )

        with self.assertRaises(ProviderActionError) as raised:
            run_provider_action(provider, action="send", page=page, func=lambda: "unused")

        self.assertEqual(raised.exception.failure.kind, "challenge_required")


if __name__ == "__main__":
    unittest.main()
