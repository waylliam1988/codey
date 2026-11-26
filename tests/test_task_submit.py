from __future__ import annotations

import os
import sys
import tempfile
import unittest
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import ParamSpec
from unittest import mock

from codey.app import task_submit
from codey.app.context import AppContext
from codey.app.run_registry import RunSnapshot
from codey.operations.task_run import TaskRunDeps
from codey.task.model import TaskSubmission
from tests import http_context

_P = ParamSpec("_P")


class _FakeState(AppContext):
    def __init__(self, reserved: bool = True) -> None:
        super().__init__(start_scheduler=False, memory_enabled=False)
        self.reserved = reserved
        self.released: list[str] = []
        self.expired: list[str] = []

    def admit_and_claim(self, session_id: str, run_id: str, task: str) -> None:
        self.durable.admit_input(
            conversation_id=session_id,
            request_id=run_id,
            task_id=run_id,
            text=task,
            provider_snapshot={"provider_id": "deepseek"},
        )
        self.durable.claim_task(run_id)

    def reserve_run(
        self, *, session_id: str, project: str | None, task: str,
        provider_id: str, run_id: str = "", abort_if_stopped: bool = False,
    ) -> RunSnapshot | None:
        if not self.reserved:
            return None
        return self.task_runtime.reserve_run(
            session_id=session_id, project=project, task=task, provider_id=provider_id,
            run_id=run_id or "run-1", abort_if_stopped=abort_if_stopped,
        )

    def release_run(self, run_id: str) -> None:
        self.released.append(run_id)

    def expire_stale_shell_approvals(self, active_run_id: str) -> None:
        self.expired.append(active_run_id)

    def close(self) -> bool:
        # Every accepted job in this fixture is a stub, with no real worker.
        for run in self.task_runtime.active_runs():
            self.task_runtime.release_run(run.run_id)
        return super().close()


@dataclass
class _BrowserQueue:
    accepted: bool = True
    error: RuntimeError | None = None
    jobs: list[Callable[[], None]] = field(default_factory=list)

    def submit(self, job: Callable[_P, None], *args: _P.args, **kwargs: _P.kwargs) -> bool:
        assert job is task_submit.run_task
        self.jobs.append(lambda: job(*args, **kwargs))
        if self.error is not None:
            raise self.error
        return self.accepted


@contextmanager
def _browser_queue(*, accepted: bool = True, error: RuntimeError | None = None) -> Generator[_BrowserQueue, None, None]:
    queue = _BrowserQueue(accepted=accepted, error=error)
    with mock.patch.object(task_submit, "submit_browser_task", new=queue.submit):
        yield queue


class TaskSubmitTests(unittest.TestCase):
    def setUp(self) -> None:
        from codey.providers import model_preferences

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        preferences = mock.patch.object(model_preferences, "DEFAULT_STATE_HOME", Path(temporary.name))
        preferences.start()
        self.addCleanup(preferences.stop)

    def test_submit_task_returns_run_id_and_expires_approvals(self) -> None:
        state = _FakeState(reserved=True)
        with _browser_queue() as queued:
            run_id = task_submit.submit_task(
                "s", None, "hello", 8, False, "deepseek", get_state=lambda: state
            )
        self.assertEqual(run_id, "run-1")
        self.assertEqual(state.expired, list[str](["run-1"]))
        self.assertEqual(state.released, list[str]())
        self.assertEqual(len(queued.jobs), 1)

    def test_submit_task_returns_none_when_no_reservation(self) -> None:
        state = _FakeState(reserved=False)
        with _browser_queue() as queued:
            self.assertIsNone(
                task_submit.submit_task("s", None, "t", 8, False, "deepseek", get_state=lambda: state)
            )
        self.assertFalse(queued.jobs)

    def test_submit_task_releases_on_worker_error(self) -> None:
        state = _FakeState(reserved=True)
        with _browser_queue(error=RuntimeError("boom")), self.assertRaises(RuntimeError):
            task_submit.submit_task("s", None, "t", 8, False, "deepseek", get_state=lambda: state)
        self.assertEqual(state.released, list[str](["run-1"]))

    def test_submit_task_retains_durable_admission_when_worker_queue_is_full(self) -> None:
        state = _FakeState(reserved=True)
        retry_calls: list[str] = []
        with _browser_queue(accepted=False), mock.patch.object(
            state.durable_scheduler, "retry", new=retry_calls.append,
        ):
            run_id = task_submit.submit_task("s", None, "t", 8, False, "deepseek", get_state=lambda: state)
        self.assertEqual(run_id, "run-1")
        self.assertEqual(state.released, list[str]())
        self.assertEqual(state.durable.task("run-1").phase, "accepted")
        active = state.run_registry.current()
        assert active is not None and not active.worker_owned
        self.assertEqual(retry_calls, list[str](["run-1"]))

    def test_submit_task_rejects_invalid_policy_before_reserve(self) -> None:
        state = _FakeState(reserved=True)
        reserve_calls: list[dict[str, object]] = []
        original_reserve = state.reserve_run

        def tracking_reserve(
            *, session_id: str, project: str | None, task: str,
            provider_id: str, run_id: str = "", abort_if_stopped: bool = False,
        ) -> RunSnapshot | None:
            reserve_calls.append(dict(session_id=session_id, project=project, task=task,
                                      provider_id=provider_id, run_id=run_id, abort_if_stopped=abort_if_stopped))
            return original_reserve(session_id=session_id, project=project, task=task,
                                    provider_id=provider_id, run_id=run_id, abort_if_stopped=abort_if_stopped)

        with mock.patch.object(state, "reserve_run", new=tracking_reserve):
            with (
                mock.patch.dict(os.environ, dict[str, str](REVIEW_POLICY="require_weeb")),
                _browser_queue() as queued,
                self.assertRaises(ValueError),
            ):
                task_submit.submit_task("s", None, "t", 8, False, "deepseek", get_state=lambda: state)
            # Fail fast: no slot taken, nothing to release, worker never queued.
            self.assertEqual(reserve_calls, list[dict[str, object]]())
            self.assertEqual(state.released, list[str]())
            self.assertFalse(queued.jobs)
            # The slot stays usable afterwards: a valid submit still reserves.
            with _browser_queue():
                run_id = task_submit.submit_task(
                    "s", None, "t", 8, False, "deepseek", get_state=lambda: state
                )
        self.assertEqual(run_id, "run-1")
        self.assertEqual(len(reserve_calls), 1)
        self.assertEqual(state.expired, list[str](["run-1"]))

    def test_run_task_releases_reserved_slot_on_policy_error(self) -> None:
        state = _FakeState(reserved=True)
        state.admit_and_claim("s", "run-9", "t")
        with (
            mock.patch.dict(os.environ, dict[str, str](REVIEW_POLICY="require_weeb")),
            self.assertRaises(ValueError),
        ):
            task_submit.run_task(
                "s", None, "t", 8, False, "deepseek", "auto", "run-9",
                get_state=lambda: state,
                attempt_token=state.durable.task("run-9").attempt_token,
            )
        self.assertEqual(state.released, list[str](["run-9"]))

    def test_run_task_releases_slot_when_lazy_import_fails(self) -> None:
        state = _FakeState(reserved=True)
        state.admit_and_claim("s", "run-9", "t")
        with (
            mock.patch.dict(sys.modules, dict[str, object]({"codey.operations.task_submission": None})),
            self.assertRaises(ImportError),
        ):
            task_submit.run_task(
                "s", None, "t", 8, False, "deepseek", "auto", "run-9",
                get_state=lambda: state,
                attempt_token=state.durable.task("run-9").attempt_token,
                review_policy="web_if_available",
            )
        self.assertEqual(state.released, list[str](["run-9"]))

    def test_worker_releases_slot_when_accessor_breaks_after_submit(self) -> None:
        state = _FakeState(reserved=True)
        broken, calls = False, 0

        def get_state() -> _FakeState:
            nonlocal calls
            calls += 1
            if broken:
                raise RuntimeError("accessor down")
            return state

        with (
            mock.patch.dict(sys.modules, dict[str, object]({"codey.operations.task_submission": None})),
            _browser_queue() as queued,
        ):
            run_id = task_submit.submit_task(
                "s", None, "t", 8, False, "deepseek", get_state=get_state
            )
            self.assertEqual(run_id, "run-1")
            # The worker uses captured state even after the accessor breaks.
            broken = True
            self.assertEqual(len(queued.jobs), 1)
            with self.assertRaises(ImportError):
                queued.jobs[0]()
            self.assertEqual(state.released, list[str](["run-1"]))
        self.assertEqual(calls, 1)

    def test_run_task_uses_captured_state_for_post_entry_cleanup(self) -> None:
        from types import ModuleType

        state = _FakeState(reserved=True)
        state.admit_and_claim("s", "run-9", "t")
        supervisor = mock.Mock()
        state._self_repair = supervisor
        kicks: list[Callable[[], bool]] = []
        entries: list[TaskSubmission] = []
        calls = 0

        def get_state() -> _FakeState:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("accessor down after entry")
            return state

        def entry(deps: TaskRunDeps, request: TaskSubmission) -> None:
            self.assertIs(deps.state, state)
            entries.append(request)

        def kick(is_busy: Callable[[], bool]) -> bool:
            kicks.append(is_busy)
            return False

        module = ModuleType("codey.operations.task_submission")
        with (
            mock.patch.object(module, "run_task_submission", new=entry, create=True),
            mock.patch.dict(sys.modules, dict[str, object]({"codey.operations.task_submission": module})),
            mock.patch.object(supervisor, "kick_if_idle", new=kick),
        ):
            task_submit.run_task(
                "s", None, "t", 8, False, "deepseek", "auto", "run-9",
                get_state=get_state,
                attempt_token=state.durable.task("run-9").attempt_token,
                review_policy="web_if_available",
            )
        self.assertEqual(calls, 1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(kicks, list[Callable[[], bool]]([state.is_busy]))

    def test_after_slot_release_returns_none_when_stopped(self) -> None:
        state = _FakeState(reserved=True)
        state.task_runtime.stop_flag.set()
        run_id = task_submit.submit_task_after_slot_release(
            "s", None, "t", 8, False, "deepseek", get_state=lambda: state
        )
        self.assertIsNone(run_id)

    def test_server_wrappers_bind_get_state(self) -> None:
        state = _FakeState()
        accessors: list[Callable[[], AppContext]] = []

        def inner(
            session_id: str, project: str | None, task: str, max_turns: int,
            continue_task: bool, provider_id: str, intent: str = "auto", *,
            get_state: Callable[[], AppContext], **kwargs: object,
        ) -> str:
            accessors.append(get_state)
            return "run-9"

        with (
            mock.patch.object(task_submit, "submit_task", new=inner),
            mock.patch("tests.http_context.get_context", new=lambda: state),
        ):
            run_id = http_context.submit("s", None, "t", 8, False, "deepseek")
        self.assertEqual(run_id, "run-9")
        self.assertEqual(len(accessors), 1)
        self.assertIs(accessors[0](), state)


if __name__ == "__main__":
    unittest.main()
