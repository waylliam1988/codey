from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from codey.app import task_submit
from codey.automation.browser_worker import BrowserWorkerBusy


def _fake_reserved(run_id="run-1"):
    return SimpleNamespace(run_id=run_id)


class _FakeState:
    def __init__(self, reserved=True):
        self.reserved = reserved
        self.released: list[str] = []
        self.expired: list[str] = []
        self.run_registry = SimpleNamespace(stop_flag=SimpleNamespace(is_set=lambda: False))

    def reserve_run(self, **kwargs):
        return _fake_reserved() if self.reserved else None

    def release_run(self, run_id):
        self.released.append(run_id)

    def expire_stale_shell_approvals(self, run_id):
        self.expired.append(run_id)


class TaskSubmitTests(unittest.TestCase):
    def test_submit_task_returns_run_id_and_expires_approvals(self) -> None:
        state = _FakeState(reserved=True)
        with mock.patch.object(task_submit, "submit_browser_task", return_value=True) as queued:
            run_id = task_submit.submit_task(
                "s", None, "hello", 8, False, "deepseek", get_state=lambda: state
            )
        self.assertEqual(run_id, "run-1")
        self.assertEqual(state.expired, ["run-1"])
        self.assertEqual(state.released, [])
        queued.assert_called_once()

    def test_submit_task_returns_none_when_no_reservation(self) -> None:
        state = _FakeState(reserved=False)
        with mock.patch.object(task_submit, "submit_browser_task") as queued:
            self.assertIsNone(
                task_submit.submit_task("s", None, "t", 8, False, "deepseek", get_state=lambda: state)
            )
        queued.assert_not_called()

    def test_submit_task_releases_on_worker_error(self) -> None:
        state = _FakeState(reserved=True)
        with mock.patch.object(
            task_submit, "submit_browser_task", side_effect=RuntimeError("boom")
        ), self.assertRaises(RuntimeError):
            task_submit.submit_task("s", None, "t", 8, False, "deepseek", get_state=lambda: state)
        self.assertEqual(state.released, ["run-1"])

    def test_submit_task_raises_busy_and_releases(self) -> None:
        state = _FakeState(reserved=True)
        with mock.patch.object(task_submit, "submit_browser_task", return_value=False), self.assertRaises(BrowserWorkerBusy):
            task_submit.submit_task("s", None, "t", 8, False, "deepseek", get_state=lambda: state)
        self.assertEqual(state.released, ["run-1"])

    def test_submit_task_rejects_invalid_policy_before_reserve(self) -> None:
        import os

        state = _FakeState(reserved=True)
        reserve_calls: list[dict] = []
        original_reserve = state.reserve_run

        def tracking_reserve(**kwargs):
            reserve_calls.append(kwargs)
            return original_reserve(**kwargs)

        state.reserve_run = tracking_reserve  # type: ignore[method-assign]
        with (
            mock.patch.dict(os.environ, {"REVIEW_POLICY": "require_weeb"}),
            mock.patch.object(task_submit, "submit_browser_task") as queued,
            self.assertRaises(ValueError),
        ):
            task_submit.submit_task("s", None, "t", 8, False, "deepseek", get_state=lambda: state)
        # Fail fast: no slot taken, nothing to release, worker never queued.
        self.assertEqual(reserve_calls, [])
        self.assertEqual(state.released, [])
        queued.assert_not_called()
        # The slot stays usable afterwards: a valid submit still reserves.
        with mock.patch.object(task_submit, "submit_browser_task", return_value=True):
            run_id = task_submit.submit_task(
                "s", None, "t", 8, False, "deepseek", get_state=lambda: state
            )
        self.assertEqual(run_id, "run-1")
        self.assertEqual(len(reserve_calls), 1)
        self.assertEqual(state.expired, ["run-1"])

    def test_run_task_releases_reserved_slot_on_policy_error(self) -> None:
        import os

        state = _FakeState(reserved=True)
        with (
            mock.patch.dict(os.environ, {"REVIEW_POLICY": "require_weeb"}),
            self.assertRaises(ValueError),
        ):
            task_submit.run_task(
                "s", None, "t", 8, False, "deepseek", "auto", "run-9",
                get_state=lambda: state,
            )
        self.assertEqual(state.released, ["run-9"])

    def test_run_task_releases_slot_when_lazy_import_fails(self) -> None:
        import sys

        state = _FakeState(reserved=True)
        with (
            mock.patch.dict(sys.modules, {"codey.operations.task_entry": None}),
            self.assertRaises(ImportError),
        ):
            task_submit.run_task(
                "s", None, "t", 8, False, "deepseek", "auto", "run-9",
                get_state=lambda: state,
                review_policy="web_if_available",
            )
        self.assertEqual(state.released, ["run-9"])

    def test_after_slot_release_returns_none_when_stopped(self) -> None:
        state = _FakeState(reserved=True)
        state.run_registry = SimpleNamespace(
            stop_flag=SimpleNamespace(is_set=lambda: True),
            wait_for_slot=mock.Mock(),
        )
        run_id = task_submit.submit_task_after_slot_release(
            "s", None, "t", 8, False, "deepseek", get_state=lambda: state
        )
        self.assertIsNone(run_id)

    def test_server_wrappers_bind_get_state(self) -> None:
        from codey.app import server

        with (
            mock.patch.object(task_submit, "submit_task", return_value="run-9") as inner,
            mock.patch.object(server, "get_state") as get_state,
        ):
            sentinel = object()
            get_state.return_value = sentinel
            run_id = server._submit_task("s", None, "t", 8, False, "deepseek")
        self.assertEqual(run_id, "run-9")
        self.assertEqual(inner.call_args.kwargs["get_state"](), sentinel)


if __name__ == "__main__":
    unittest.main()
