"""Preset run_id must not bypass the single-slot busy check, nor be recycled."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.app.run_registry import RunSnapshot
from codey.operations.task_run import prepare_submission
from codey.task.model import TaskSubmission


@dataclass
class _FakeState:
    active: RunSnapshot | None = None
    reserved: RunSnapshot | None = None

    def current_run(self) -> RunSnapshot | None:
        return self.active

    def reserve_run(self, *, session_id: str, project, task, provider_id, run_id: str = ""):
        if self.active is not None:
            return None
        run = RunSnapshot(
            run_id=run_id or "run_new",
            session_id=session_id,
            project=project,
            task=task,
            provider_id=provider_id,
        )
        self.reserved = run
        return run


def _submission(run_id: str = "") -> TaskSubmission:
    return TaskSubmission(
        session_id="session-1",
        project=None,
        task="hello",
        max_turns=3,
        continue_task=False,
        provider_id="qwen",
        run_id=run_id,
    )


class PrepareSubmissionTests(unittest.TestCase):
    def test_preset_run_id_matching_active_run_is_allowed(self) -> None:
        active = RunSnapshot(
            run_id="run-a", session_id="session-1", project=None, task="t", provider_id="qwen"
        )
        result = prepare_submission(_FakeState(active=active), _submission("run-a"))

        assert result is not None
        self.assertEqual(result.run_id, "run-a")

    def test_preset_run_id_for_other_run_is_busy(self) -> None:
        active = RunSnapshot(
            run_id="run-a", session_id="session-1", project=None, task="t", provider_id="qwen"
        )
        self.assertIsNone(prepare_submission(_FakeState(active=active), _submission("run-b")))

    def test_empty_run_id_still_reserves(self) -> None:
        state = _FakeState()
        result = prepare_submission(state, _submission(""))

        assert result is not None
        self.assertEqual(result.run_id, "run_new")


class HeadlessRunIdReuseTests(unittest.TestCase):
    def test_reused_run_id_is_rejected_without_running_agent(self) -> None:
        from codey.agents.request import AgentRequest
        from codey.agents.runner import RunResult

        calls: list[str] = []

        def fake_agent(request: AgentRequest):
            calls.append("agent")
            return RunResult("finished", "done", 1, checks_passed=True, checks_ran=True)

        class _FakeProvider:
            name = "Fake"

            def close(self) -> None:
                pass

        def provider(*_args, **_kwargs):
            return _FakeProvider()

        with tempfile.TemporaryDirectory() as td:
            state_home = Path(td, "state")
            first_rows: list[dict] = []
            first = run_headless(
                HeadlessRequest(
                    project=Path(td, "project"),
                    task="first",
                    provider_id="qwen",
                    max_turns=2,
                    session_id="session-dup",
                    run_id="run-fixed",
                    state_home=state_home,
                ),
                emit_jsonl=first_rows.append,
                agent_run=fake_agent,
                collect_changes=lambda *_a, **_k: {"ok": True, "changed_count": 0, "files": [], "diff": ""},
                connect_provider=provider,
            )
            # First run uses a fake agent that never touches the provider.
            self.assertEqual(first.run_id, "run-fixed")

            calls.clear()
            second_rows: list[dict] = []
            second = run_headless(
                HeadlessRequest(
                    project=Path(td, "project"),
                    task="second",
                    provider_id="qwen",
                    max_turns=2,
                    session_id="session-dup",
                    run_id="run-fixed",
                    state_home=state_home,
                ),
                emit_jsonl=second_rows.append,
                agent_run=fake_agent,
                collect_changes=lambda *_a, **_k: {"ok": True, "changed_count": 0, "files": [], "diff": ""},
                connect_provider=provider,
            )

            self.assertEqual(second.exit_code, 1)
            self.assertEqual(second.run_id, "run-fixed")
            self.assertIn(second.stop_reason, ("duplicate", "busy"))
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
