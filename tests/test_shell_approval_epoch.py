"""Allow/Stop race: a Stop landing after Allow claimed must win (no Popen)."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from codey.app import api as app_api
from codey.app import services as app_services


class _FakeRegistry:
    def __init__(self) -> None:
        self.stop_flag = threading.Event()


class _FakeCtx:
    def __init__(self, pending: dict, generation_at_pop: int, generation_now: int) -> None:
        self._pending = pending
        self._generation_at_pop = generation_at_pop
        self._generation_now = generation_now
        self.run_registry = _FakeRegistry()
        self.recorded: list[dict] = []

    def pop_pending_shell_approval(self, approval_id: str) -> dict | None:
        pending = dict(self._pending)
        pending["_approval_generation"] = self._generation_at_pop
        return pending

    def approval_generation(self) -> int:
        return self._generation_now

    def record_shell_result(self, event: dict) -> None:
        self.recorded.append(event)


def _pending() -> dict:
    return {
        "id": "shell-1",
        "session_id": "session-1",
        "run_id": "run-1",
        "command": "pytest -q",
        "cwd": ".",
        "project": None,
    }


class ShellApprovalEpochTests(unittest.TestCase):
    def test_stale_claim_returns_stopped_without_executing(self) -> None:
        ctx = _FakeCtx(_pending(), generation_at_pop=3, generation_now=4)
        with mock.patch.object(
            app_api.services, "execute_approved_shell", side_effect=AssertionError("must not execute")
        ):
            status, payload = app_api.shell_approval_response(
                ctx,
                {"id": "shell-1", "approved": True},
                submit_task_after_slot_release=lambda *args, **kwargs: None,
            )

        self.assertEqual(status, 409)
        self.assertEqual(payload.get("error"), "stopped")
        self.assertEqual(len(ctx.recorded), 1)
        self.assertFalse(ctx.recorded[0]["approved"])

    def test_stop_flag_set_returns_stopped_without_executing(self) -> None:
        ctx = _FakeCtx(_pending(), generation_at_pop=7, generation_now=7)
        ctx.run_registry.stop_flag.set()
        with mock.patch.object(
            app_api.services, "execute_approved_shell", side_effect=AssertionError("must not execute")
        ):
            status, _ = app_api.shell_approval_response(
                ctx,
                {"id": "shell-1", "approved": True},
                submit_task_after_slot_release=lambda *args, **kwargs: None,
            )

        self.assertEqual(status, 409)

    def test_execute_refuses_stale_generation_before_popen(self) -> None:
        ctx = _FakeCtx(_pending(), generation_at_pop=1, generation_now=2)
        with mock.patch(
            "codey.runtime.core.cancellation.run_process",
            side_effect=AssertionError("Popen must not start"),
        ):
            result = app_services.execute_approved_shell(
                ctx, ".", ".", "pytest -q", expected_approval_generation=1
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "command stopped")

    def test_execute_refuses_set_stop_flag_before_popen(self) -> None:
        ctx = _FakeCtx(_pending(), generation_at_pop=5, generation_now=5)
        ctx.run_registry.stop_flag.set()
        with mock.patch(
            "codey.runtime.core.cancellation.run_process",
            side_effect=AssertionError("Popen must not start"),
        ):
            result = app_services.execute_approved_shell(
                ctx, ".", ".", "pytest -q", expected_approval_generation=5
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "command stopped")


    def test_stopped_execute_result_is_denied_without_continuation(self) -> None:
        pending = _pending()
        pending["continue_after"] = True
        ctx = _FakeCtx(pending, generation_at_pop=9, generation_now=9)
        submit = mock.Mock()
        with mock.patch.object(
            app_api.services,
            "execute_approved_shell",
            return_value={
                "ok": False,
                "error": "command stopped",
                "exit_code": None,
                "output": "",
                "stopped": True,
            },
        ):
            status, payload = app_api.shell_approval_response(
                ctx,
                {"id": "shell-1", "approved": True},
                submit_task_after_slot_release=submit,
            )

        self.assertEqual(status, 409)
        self.assertEqual(payload.get("error"), "stopped")
        self.assertEqual(len(ctx.recorded), 1)
        self.assertFalse(ctx.recorded[0]["approved"])
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
