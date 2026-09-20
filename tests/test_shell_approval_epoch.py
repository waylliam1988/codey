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
        self.lock = threading.Lock()
        self._shell_spawn_gate = threading.Lock()

    def pop_pending_shell_approval(self, approval_id: str) -> dict | None:
        pending = dict(self._pending)
        pending["_approval_generation"] = self._generation_at_pop
        return pending

    def claim_shell_ticket(
        self, approval_id: str, *, timeout: int, output_limit: int
    ) -> tuple[dict | None, object | None]:
        from codey.app.services import ShellExecutionTicket

        pending = dict(self._pending)
        claimed = self._generation_at_pop
        pending.pop("_approval_generation", None)
        if self.run_registry.stop_flag.is_set() or claimed != self._generation_now:
            return pending, None
        ticket = ShellExecutionTicket(
            command=str(pending.get("command") or ""),
            cwd=__import__("pathlib").Path(".").resolve(),
            generation=claimed,
            timeout=timeout,
            output_limit=output_limit,
        )
        return pending, ticket

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
            app_api.services, "execute_shell_ticket", side_effect=AssertionError("must not execute")
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
            app_api.services, "execute_shell_ticket", side_effect=AssertionError("must not execute")
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
            "codey.runtime.core.cancellation.start_process",
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
            "codey.runtime.core.cancellation.start_process",
            side_effect=AssertionError("Popen must not start"),
        ):
            result = app_services.execute_approved_shell(
                ctx, ".", ".", "pytest -q", expected_approval_generation=5
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "command stopped")


    def test_stop_between_final_check_and_popen_is_refused(self) -> None:
        """Spawn gate: Stop bumping the epoch before Popen must still win."""
        import pathlib

        ctx = _FakeCtx(_pending(), generation_at_pop=5, generation_now=5)
        ticket = app_services.ShellExecutionTicket(
            command="pytest -q",
            cwd=pathlib.Path(".").resolve(),
            generation=5,
            timeout=30,
            output_limit=4000,
        )

        def _bumped_generation() -> int:
            # Simulate Stop landing between claim and Popen: epoch moves.
            return 6

        with (
            mock.patch.object(ctx, "approval_generation", side_effect=_bumped_generation),
            mock.patch.object(
                app_services.cancellation,
                "start_process",
                side_effect=AssertionError("Popen must not start"),
            ),
        ):
            result = app_services.execute_shell_ticket(ctx, ticket)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "command stopped")

    def test_stopped_execute_result_is_denied_without_continuation(self) -> None:
        pending = _pending()
        pending["continue_after"] = True
        ctx = _FakeCtx(pending, generation_at_pop=9, generation_now=9)
        submit = mock.Mock()
        with mock.patch.object(
            app_api.services,
            "execute_shell_ticket",
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
