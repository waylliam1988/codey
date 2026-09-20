"""Allow/Stop race: a Stop landing after Allow claimed must win (no Popen)."""

from __future__ import annotations

import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from codey.app import api as app_api
from codey.app import shell_service
from codey.runtime.core import cancellation


class _FakeRegistry:
    def __init__(self) -> None:
        self.stop_flag = threading.Event()


class _FakeCtx:
    def __init__(self, pending: dict, generation_at_pop: int, generation_now: int) -> None:
        from codey.app.approval_registry import ApprovalRegistry

        self._generation_now = generation_now
        self.run_registry = _FakeRegistry()
        self.recorded: list[dict] = []
        self.lock = threading.Lock()
        self._shell_spawn_gate = threading.Lock()
        # Production claim path needs a real registry: seed the pop-time
        # generation on the record and the read-time generation on the
        # counter directly (a Stop between them is what these tests pin).
        self.approvals = ApprovalRegistry()
        self.approvals.add_shell("shell-1", dict(pending))
        self.approvals._pending_shell["shell-1"]["_approval_generation"] = generation_at_pop
        self.approvals._generation = generation_now

    def pop_pending_shell_approval(self, approval_id: str) -> dict | None:
        return self.approvals.pop_shell(approval_id)

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
            shell_service, "execute_shell_ticket", side_effect=AssertionError("must not execute")
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
            shell_service, "execute_shell_ticket", side_effect=AssertionError("must not execute")
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
            result = shell_service.execute_approved_shell(
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
            result = shell_service.execute_approved_shell(
                ctx, ".", ".", "pytest -q", expected_approval_generation=5
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "command stopped")


    def test_stop_between_final_check_and_popen_is_refused(self) -> None:
        """Spawn gate: Stop bumping the epoch before Popen must still win."""
        import pathlib

        ctx = _FakeCtx(_pending(), generation_at_pop=5, generation_now=5)
        ticket = shell_service.ShellExecutionTicket(
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
                cancellation,
                "start_process",
                side_effect=AssertionError("Popen must not start"),
            ),
        ):
            result = shell_service.execute_shell_ticket(ctx, ticket)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "command stopped")

    def test_stopped_execute_result_is_denied_without_continuation(self) -> None:
        pending = _pending()
        pending["continue_after"] = True
        ctx = _FakeCtx(pending, generation_at_pop=9, generation_now=9)
        submit = mock.Mock()
        with mock.patch.object(
            shell_service,
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


class ShellStopLinearizationTests(unittest.TestCase):
    """request_stop() and the spawn gate linearize Stop vs Allow.

    Uses a real AppContext: the gate, generation, and stop flag are the
    production objects, so these tests pin the cross-thread contract instead
    of the FakeCtx approximation above.
    """

    def _ctx(self) -> object:
        from codey.app import server

        return server.AppContext()

    def _ticket(self, ctx: object, project: str):
        pending = {
            "id": "shell-1",
            "session_id": "session-1",
            "run_id": "run-1",
            "command": "pytest -q",
            "cwd": ".",
            "project": project,
        }
        ctx.add_pending_shell_approval("shell-1", pending)
        claimed, ticket = shell_service.claim_shell_ticket(
            ctx, "shell-1", timeout=30, output_limit=4000
        )
        self.assertIsNotNone(claimed)
        self.assertIsNotNone(ticket)
        return ticket

    def test_stop_before_execute_refuses_without_spawn(self) -> None:
        ctx = self._ctx()
        try:
            with tempfile.TemporaryDirectory() as td:
                ticket = self._ticket(ctx, td)
                ctx.request_stop()
                with mock.patch.object(
                    cancellation,
                    "start_process",
                    side_effect=AssertionError("Popen must not start"),
                ):
                    result = shell_service.execute_shell_ticket(ctx, ticket)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "command stopped")
        finally:
            ctx.close()

    def test_overlapped_stop_never_completes_successfully(self) -> None:
        ctx = self._ctx()
        try:
            with tempfile.TemporaryDirectory() as td:
                ticket = self._ticket(ctx, td)
                proc = mock.Mock()
                stop_seen: list[bool] = []

                def _slow_spawn(*args, **kwargs):
                    # Widen the old check->Popen window; a production Stop
                    # (request_stop) contends on the gate from another thread.
                    time.sleep(0.3)
                    return proc, None

                def _stop_aware_wait(proc_arg, _job, _args, _timeout):
                    # The gate is released before waiting, so Stop always
                    # gets in first: joining here is deadlock-free.
                    stopper.join(timeout=10)
                    stop_seen.append(ctx.run_registry.stop_flag.is_set())
                    raise cancellation.TaskCancelled("stop")

                stopper = threading.Thread(target=ctx.request_stop, daemon=True)
                with (
                    mock.patch.object(
                        cancellation, "start_process", side_effect=_slow_spawn
                    ) as spawn_mock,
                    mock.patch.object(
                        cancellation, "wait_process", side_effect=_stop_aware_wait
                    ),
                ):
                    stopper.start()
                    time.sleep(0.05)
                    result = shell_service.execute_shell_ticket(ctx, ticket)
                    stopper.join(timeout=10)
            # Linearization allows exactly two outcomes, both non-success:
            # Stop wins the gate (refused before spawn) or loses it (spawned
            # but terminated: wait observes the flag).
            self.assertFalse(result.get("ok", False))
            self.assertFalse(stopper.is_alive())
            if spawn_mock.called:
                self.assertTrue(stop_seen and stop_seen[0])
        finally:
            ctx.close()

    def test_close_waits_on_the_gate_and_sets_flag_late(self) -> None:
        """close() linearizes like every other Stop: while the executor holds
        the spawn gate inside a stuck spawn, a concurrent close() must stay
        blocked with the stop flag still clear. Nails the gate against a
        future "optimization" back to a bare set()."""
        ctx = self._ctx()
        try:
            with tempfile.TemporaryDirectory() as td:
                ticket = self._ticket(ctx, td)
                entered: list[bool] = []
                release = threading.Event()

                def _stuck_spawn(*args, **kwargs):
                    entered.append(True)
                    self.assertTrue(release.wait(timeout=10))
                    return mock.Mock(), None

                completed = subprocess.CompletedProcess("pytest -q", 0, "out", "")
                with (
                    mock.patch.object(
                        cancellation, "start_process", side_effect=_stuck_spawn
                    ),
                    mock.patch.object(
                        cancellation, "wait_process", return_value=completed
                    ),
                ):
                    executor = threading.Thread(
                        target=shell_service.execute_shell_ticket,
                        args=(ctx, ticket),
                        daemon=True,
                    )
                    executor.start()
                    deadline = time.monotonic() + 10
                    while not entered and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(entered, "executor never reached spawn")
                    closer = threading.Thread(target=ctx.close, daemon=True)
                    closer.start()
                    time.sleep(0.3)
                    # Still gated: close cannot set the flag from outside.
                    self.assertTrue(closer.is_alive(), "close slipped past the gate")
                    self.assertFalse(ctx.run_registry.stop_flag.is_set())
                    release.set()
                    executor.join(timeout=10)
                    closer.join(timeout=10)
                    self.assertFalse(executor.is_alive())
                    self.assertFalse(closer.is_alive())
                    self.assertTrue(ctx.run_registry.stop_flag.is_set())
                    self.assertTrue(ctx.closed)
        finally:
            ctx.close()

    def test_executor_holds_the_gate_across_check_and_spawn(self) -> None:
        ctx = self._ctx()
        try:
            with tempfile.TemporaryDirectory() as td:
                ticket = self._ticket(ctx, td)
                entered: list[bool] = []
                release = threading.Event()

                def _gated_spawn(*args, **kwargs):
                    entered.append(True)
                    self.assertTrue(release.wait(timeout=10))
                    return mock.Mock(), None

                completed = subprocess.CompletedProcess("pytest -q", 0, "out", "")
                with (
                    mock.patch.object(
                        cancellation, "start_process", side_effect=_gated_spawn
                    ),
                    mock.patch.object(
                        cancellation, "wait_process", return_value=completed
                    ),
                ):
                    worker = threading.Thread(
                        target=shell_service.execute_shell_ticket,
                        args=(ctx, ticket),
                        daemon=True,
                    )
                    worker.start()
                    # The executor must reach the gated spawn before Stop can
                    # interleave between the final check and Popen.
                    deadline = time.monotonic() + 10
                    while not entered and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(entered, "executor never reached spawn")
                    self.assertFalse(ctx.run_registry.stop_flag.is_set())
                    release.set()
                    worker.join(timeout=10)
                    self.assertFalse(worker.is_alive())
        finally:
            ctx.close()


if __name__ == "__main__":
    unittest.main()
