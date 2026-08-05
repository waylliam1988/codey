from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codey.agent import RunResult
from codey.events import MAX_EVENT_TEXT_CHARS, RunEvent
from codey.headless_runner import HeadlessRequest, headless_event_payload, run_headless
from codey.models import ToolCall
from codey.tool_runtime import ToolOutcome


class _FakeProvider:
    name = "Fake"

    def close(self) -> None:
        pass


class HeadlessRunnerTests(unittest.TestCase):
    def test_project_task_emits_jsonl_and_writes_ledger(self) -> None:
        rows: list[dict[str, object]] = []

        def fake_agent(*_args, **kwargs):
            on_event = kwargs["on_event"]
            call = ToolCall("run", {"path": ".", "command": "python -m pytest -q"})
            on_event(RunEvent.turn_started(1, '{"tool":"run","args":{}}'))
            on_event(RunEvent.tool_started(1, call, "Running tests"))
            on_event(RunEvent.tool_finished(
                1,
                call,
                ToolOutcome("ok", True, exit_code=0),
            ))
            return RunResult("finished", "done", 1, checks_passed=True, checks_ran=True)

        with tempfile.TemporaryDirectory() as td:
            project = Path(td, "project")
            result = run_headless(
                HeadlessRequest(
                    project=project,
                    task="check tests",
                    provider_id="qwen",
                    max_turns=3,
                    session_id="session-1",
                    state_home=Path(td, "state"),
                ),
                emit_jsonl=rows.append,
                agent_run=fake_agent,
                collect_changes=lambda *_args, **_kwargs: {"ok": True, "changed_count": 0, "files": [], "diff": ""},
                connect_provider=lambda *_args, **_kwargs: _FakeProvider(),
            )
            ledger_exists = Path(result.ledger_path).exists()

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stop_reason, "done")
        self.assertTrue(result.ledger_path)
        self.assertTrue(ledger_exists)
        self.assertEqual(rows[0]["type"], "task_start")
        self.assertEqual(rows[0]["mode"], "agent")
        tool = next(row for row in rows if row["type"] == "tool")
        self.assertEqual(tool["tool"], "run")
        self.assertTrue(tool["ok"])
        self.assertEqual(tool["exit_code"], 0)
        self.assertEqual(rows[-1]["type"], "task_done")
        self.assertEqual(rows[-1]["stop_reason"], "done")
        self.assertEqual(rows[-1]["mode"], "agent")
        self.assertEqual(rows[-1]["ledger_path"], result.ledger_path)

    def test_custom_run_id_is_pre_reserved_instead_of_noop(self) -> None:
        rows: list[dict[str, object]] = []
        calls = 0

        def fake_agent(*_args, **kwargs):
            nonlocal calls
            calls += 1
            kwargs["on_event"](RunEvent.status("running custom id"))
            return RunResult("finished", "done", 1)

        with tempfile.TemporaryDirectory() as td:
            result = run_headless(
                HeadlessRequest(
                    project=Path(td, "project"),
                    task="custom run",
                    provider_id="qwen",
                    max_turns=2,
                    session_id="session-1",
                    run_id="custom_run",
                    state_home=Path(td, "state"),
                ),
                emit_jsonl=rows.append,
                agent_run=fake_agent,
                collect_changes=lambda *_args, **_kwargs: {"ok": True, "changed_count": 0, "files": [], "diff": ""},
                connect_provider=lambda *_args, **_kwargs: _FakeProvider(),
            )
            ledger_exists = Path(result.ledger_path).exists()

        self.assertEqual(calls, 1)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.run_id, "custom_run")
        self.assertTrue(ledger_exists)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["run_id"], "custom_run")
        self.assertEqual(rows[-1]["run_id"], "custom_run")

    def test_shell_request_is_rejected_and_exits_nonzero(self) -> None:
        rows: list[dict[str, object]] = []

        def fake_agent(*_args, **kwargs):
            kwargs["on_shell_request"](".", "python setup.py install")
            return RunResult("shell command requires approval", "approval", 1)

        with tempfile.TemporaryDirectory() as td:
            result = run_headless(
                HeadlessRequest(
                    project=Path(td, "project"),
                    task="install dependency",
                    provider_id="qwen",
                    max_turns=3,
                    session_id="session-1",
                    state_home=Path(td, "state"),
                ),
                emit_jsonl=rows.append,
                agent_run=fake_agent,
                collect_changes=lambda *_args, **_kwargs: {"ok": True, "changed_count": 0, "files": [], "diff": ""},
                connect_provider=lambda *_args, **_kwargs: _FakeProvider(),
            )

        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stop_reason, "approval")
        rejected = next(row for row in rows if row["type"] == "shell_rejected")
        self.assertEqual(rejected["reason"], "headless_default_deny")
        self.assertEqual(rejected["command"], "python setup.py install")

    def test_readonly_planning_uses_readonly_profile_without_change_tracker(self) -> None:
        seen: dict[str, object] = {}

        def fake_agent(*_args, **kwargs):
            seen["permission_profile"] = kwargs.get("permission_profile")
            seen["change_tracker"] = kwargs.get("change_tracker")
            seen["project_map"] = kwargs.get("project_map")
            return RunResult("plan only", "done", 1)

        with tempfile.TemporaryDirectory() as td:
            result = run_headless(
                HeadlessRequest(
                    project=Path(td, "project"),
                    task="explain architecture",
                    provider_id="qwen",
                    max_turns=3,
                    session_id="session-1",
                    intent="planning_readonly",
                    state_home=Path(td, "state"),
                ),
                emit_jsonl=lambda _row: None,
                agent_run=fake_agent,
                collect_changes=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("readonly planning must not collect changes")
                ),
                connect_provider=lambda *_args, **_kwargs: _FakeProvider(),
            )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(seen["permission_profile"], "planning_readonly")
        self.assertIsNone(seen["change_tracker"])
        self.assertIn("Project Map", str(seen["project_map"]))

    def test_headless_event_payload_clips_large_fields(self) -> None:
        payload = headless_event_payload({
            "type": "tool",
            "run_id": "run-1",
            "session_id": "session-1",
            "kind": "run",
            "result": "x" * 2_000,
            "command": "python -m pytest " + ("x" * 2_000),
        })

        assert payload is not None
        self.assertLessEqual(len(str(payload["result"])), 200)
        self.assertLessEqual(len(str(payload["command"])), MAX_EVENT_TEXT_CHARS)

    def test_emit_jsonl_rows_are_json_serializable(self) -> None:
        rows: list[dict[str, object]] = []

        with tempfile.TemporaryDirectory() as td:
            result = run_headless(
                HeadlessRequest(
                    project=Path(td, "project"),
                    task="done",
                    provider_id="qwen",
                    max_turns=1,
                    session_id="session-1",
                    state_home=Path(td, "state"),
                ),
                emit_jsonl=rows.append,
                agent_run=lambda *_args, **_kwargs: RunResult("ok", "done", 1),
                collect_changes=lambda *_args, **_kwargs: {"ok": True, "changed_count": 0, "files": [], "diff": ""},
                connect_provider=lambda *_args, **_kwargs: _FakeProvider(),
            )

        self.assertEqual(result.exit_code, 0)
        for row in rows:
            json.dumps(row, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
