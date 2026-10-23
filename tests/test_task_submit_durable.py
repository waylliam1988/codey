from __future__ import annotations

from pathlib import Path
from unittest import mock

from codey.app import task_submit
from codey.app.context import AppContext
from codey.task.model import StartTask


def test_submit_commits_input_and_task_before_worker_dispatch(tmp_path: Path) -> None:
    state = AppContext(tmp_path / "state")
    observed: list[tuple[str, tuple[str, ...]]] = []

    def dispatch(*_args: object, **_kwargs: object) -> bool:
        entries = state.durable.entries("session-1")
        tasks = state.durable.store.pending_tasks()
        observed.append((entries[-1].kind, tuple(task.task_id for task in tasks)))
        return True

    try:
        with mock.patch.object(task_submit, "submit_browser_task", side_effect=dispatch):
            run_id = task_submit.submit_task(
                "session-1",
                None,
                "write the implementation",
                8,
                False,
                "deepseek",
                get_state=lambda: state,
                input=StartTask("write the implementation"),
            )

        assert run_id is not None
        assert observed == [("user_message", (run_id,))]
        assert state.durable.task(run_id).phase == "dispatching"
        assert state.durable.entries("session-1")[-1].payload["text"] == "write the implementation"
    finally:
        state.close()


def test_admission_freezes_task_kind_in_durable_checkpoint(tmp_path: Path) -> None:
    state = AppContext(tmp_path / "state")
    try:
        with mock.patch.object(task_submit, "submit_browser_task", return_value=True):
            run_id = task_submit.submit_task(
                "session-1",
                str(tmp_path / "project"),
                "investigate the code",
                8,
                False,
                "deepseek",
                intent="research",
                get_state=lambda: state,
                input=StartTask("investigate the code"),
            )
        assert run_id is not None
        checkpoint = state.durable.task(run_id)
        assert checkpoint.state["task_kind"] == "research"
    finally:
        state.close()


def test_worker_queue_receives_the_durable_attempt_identity(tmp_path: Path) -> None:
    state = AppContext(tmp_path / "state")
    try:
        with mock.patch.object(task_submit, "submit_browser_task", return_value=True) as queue:
            run_id = task_submit.submit_task("session", None, "work", 8, False, "deepseek", get_state=lambda: state)
        assert run_id is not None
        token = state.durable.task(run_id).attempt_token
        assert token
        assert queue.call_args.kwargs["attempt_token"] == token
    finally:
        state.close()


def test_stop_between_reservation_and_durable_admission_never_dispatches_worker(tmp_path):
    state = AppContext(tmp_path / 'state')
    original = state.durable.admit_input
    def admission(**kwargs):
        state.request_stop(kwargs['task_id'])
        return original(**kwargs)
    try:
        with mock.patch.object(state.durable, 'admit_input', side_effect=admission), mock.patch.object(task_submit, 'submit_browser_task') as worker:
            run = task_submit.submit_task('session', None, 'work', 8, False, 'deepseek', get_state=lambda: state, run_id='cancel-during-admission')
        assert run == 'cancel-during-admission'
        assert state.durable.task(run).state['cancel_requested']
        assert state.run_registry.active_for(session_id='session') is None
        worker.assert_not_called()
    finally:
        state.close()


def test_unadmitted_stop_does_not_pause_or_cancel_another_session(tmp_path):
    state = AppContext(tmp_path / 'state')
    try:
        with mock.patch.object(state.input_queue,'pause') as pause, mock.patch.object(state.shell_approval_workflow,'cancel_queued') as cancel:
            state.request_stop('not-admitted-yet')
        pause.assert_not_called()
        cancel.assert_not_called()
        with mock.patch.object(task_submit,'submit_browser_task') as worker:
            assert task_submit.submit_task('a',None,'work',8,False,'deepseek',get_state=lambda:state,run_id='not-admitted-yet') is None
        worker.assert_not_called()
    finally:
        state.close()
