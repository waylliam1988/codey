from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.agents.request import AgentRequest, ShellApprovalRequest
from codey.agents.runner import RunResult
from codey.agents.shell_approval import MAX_APPROVAL_COMMAND_CHARS
from codey.app.headless_runner import HeadlessAppContext, HeadlessRequest, headless_event_payload, run_headless
from codey.runtime.core.models import ToolCall
from codey.runtime.core.operation_state import RuntimeOperationStore
from codey.runtime.log.session_log import RuntimeSessionLog
from codey.runtime.observe.events import MAX_EVENT_TEXT_CHARS, RunEvent
from codey.toolchain.runtime import ToolOutcome


class _FakeProvider:
    name = "Fake"

    def close(self) -> None:
        pass


class _RouteProvider:
    name = "Router"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompt = ""
        self.closed = False

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        self.prompt = text
        return self.reply

    def close(self) -> None:
        self.closed = True


class HeadlessRunnerTests(unittest.TestCase):
    def test_project_task_emits_jsonl_and_writes_ledger(self) -> None:
        rows: list[dict[str, object]] = []

        def fake_agent(request: AgentRequest):
            on_event = request.on_event
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

    def test_project_task_commits_runtime_operation_terminal(self) -> None:
        rows: list[dict[str, object]] = []

        def fake_agent(_request: AgentRequest):
            return RunResult("finished", "done", 1, checks_passed=True, checks_ran=True)

        with tempfile.TemporaryDirectory() as td:
            state_home = Path(td, "state")
            result = run_headless(
                HeadlessRequest(
                    project=Path(td, "project"),
                    task="check tests",
                    provider_id="qwen",
                    max_turns=3,
                    session_id="session-op",
                    state_home=state_home,
                ),
                emit_jsonl=rows.append,
                agent_run=fake_agent,
                collect_changes=lambda *_args, **_kwargs: {"ok": True, "changed_count": 0, "files": [], "diff": ""},
                connect_provider=lambda *_args, **_kwargs: _FakeProvider(),
            )
            operation = RuntimeOperationStore(RuntimeSessionLog(state_home)).load(
                "session-op",
                result.run_id,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIsNotNone(operation)
        assert operation is not None
        self.assertEqual(operation.leaf, "terminal")
        assert operation.terminal is not None
        self.assertEqual(operation.terminal.stop_reason, "done")
        self.assertEqual(operation.terminal.provider, "qwen")

    def test_custom_run_id_is_pre_reserved_instead_of_noop(self) -> None:
        rows: list[dict[str, object]] = []
        calls = 0

        def fake_agent(request: AgentRequest):
            nonlocal calls
            calls += 1
            request.on_event(RunEvent.status("running custom id"))
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

        def fake_agent(request: AgentRequest):
            assert request.on_shell_request is not None
            request.on_shell_request(ShellApprovalRequest(
                cwd=".",
                command="python setup.py install",
            ))
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

    def test_shell_rejected_bounds_command_preview_and_keeps_full_hash(self) -> None:
        rows: list[dict[str, object]] = []
        command = "python -c \"print('" + ("x" * 1600) + "')\""

        def fake_agent(request: AgentRequest):
            assert request.on_shell_request is not None
            request.on_shell_request(ShellApprovalRequest(
                cwd=".",
                command=command,
            ))
            return RunResult("shell command requires approval", "approval", 1)

        with tempfile.TemporaryDirectory() as td:
            result = run_headless(
                HeadlessRequest(
                    project=Path(td, "project"),
                    task="run long shell",
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
        rejected = next(row for row in rows if row["type"] == "shell_rejected")
        self.assertLessEqual(len(str(rejected["command"])), MAX_APPROVAL_COMMAND_CHARS)
        self.assertTrue(rejected["command_truncated"])
        self.assertRegex(str(rejected["command_sha256"]), r"^[0-9a-f]{64}$")
        self.assertIn("[truncated; command_sha256=", str(rejected["command"]))

    def test_readonly_planning_uses_readonly_profile_without_change_tracker(self) -> None:
        seen: dict[str, object] = {}

        def fake_agent(request: AgentRequest):
            seen["permission_profile"] = request.permission_profile
            seen["change_tracker"] = request.change_tracker
            seen["project_map"] = request.project_map
            seen["project_config_warnings"] = request.project_config_warnings
            return RunResult("plan only", "done", 1)

        with tempfile.TemporaryDirectory() as td:
            project = Path(td, "project")
            config = project / ".codey" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text("{bad json", encoding="utf-8")
            result = run_headless(
                HeadlessRequest(
                    project=project,
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
        self.assertIn("invalid JSON", str(seen["project_config_warnings"]))

    def test_auto_intent_uses_router_before_headless_task_mode(self) -> None:
        """Unified auto: the first normal call requests planning; the retired
        fresh-provider router is never constructed and no second routing call
        happens."""
        seen: dict[str, object] = {}
        rows: list[dict[str, object]] = []
        main_provider = _RouteProvider(
            "ACTION: planning_readonly\nPLAN: 只给方案不改代码"
        )

        def fake_agent(request: AgentRequest):
            seen["permission_profile"] = request.permission_profile
            seen["change_tracker"] = request.change_tracker
            return RunResult("plan only", "done", 1)

        def forbid_fresh_provider(*_args, **_kwargs):
            raise AssertionError("retired router must not run")

        with tempfile.TemporaryDirectory() as td:
            result = run_headless(
                HeadlessRequest(
                    project=Path(td, "project"),
                    task="先别改代码，只给我一个方案",
                    provider_id="qwen",
                    max_turns=3,
                    session_id="session-1",
                    intent="auto",
                    state_home=Path(td, "state"),
                ),
                emit_jsonl=rows.append,
                agent_run=fake_agent,
                collect_changes=lambda *_args, **_kwargs: {
                    "ok": True,
                    "changed_count": 0,
                    "files": [],
                    "diff": "",
                },
                connect_provider=lambda *_args, **_kwargs: main_provider,
                connect_fresh_provider=forbid_fresh_provider,
            )

        self.assertEqual(result.exit_code, 0)
        # task_start still shows the deferred baseline; the terminal event
        # carries the decided planning mode after the first normal call.
        self.assertEqual(rows[0]["mode"], "agent")
        self.assertEqual(rows[-1]["mode"], "planning")
        self.assertEqual(seen["permission_profile"], "planning_readonly")
        self.assertIsNone(seen["change_tracker"])
        self.assertNotIn("Ghost", main_provider.prompt)

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

    def test_headless_run_exception_bubbles_and_closes_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td, "project")
            state_home = Path(td, "state")
            with mock.patch("codey.app.headless_runner.run_task_submission", side_effect=RuntimeError("boom in submission")), mock.patch.object(HeadlessAppContext, "close", autospec=True) as mock_close:
                with self.assertRaises(RuntimeError) as cm:
                    run_headless(
                        HeadlessRequest(
                            project=project,
                            task="failing task",
                            provider_id="qwen",
                            state_home=state_home,
                        ),
                        emit_jsonl=lambda _row: None,
                        connect_provider=lambda *_args, **_kwargs: _FakeProvider(),
                    )
                self.assertIn("boom in submission", str(cm.exception))
                mock_close.assert_called_once()

    def test_headless_double_close_failure_fails_successful_run(self) -> None:
        from codey.agents.runner import RunResult as _RunResult

        rows: list[dict[str, object]] = []
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(HeadlessAppContext, "close", return_value=False) as mock_close,
        ):
            result = run_headless(
                HeadlessRequest(
                    project=Path(td, "project"),
                    task="done task",
                    provider_id="qwen",
                    max_turns=1,
                    session_id="session-close-fail",
                    state_home=Path(td, "state"),
                ),
                emit_jsonl=rows.append,
                agent_run=lambda *_args, **_kwargs: _RunResult("ok", "done", 1),
                collect_changes=lambda *_args, **_kwargs: {"ok": True, "changed_count": 0, "files": [], "diff": ""},
                connect_provider=lambda *_args, **_kwargs: _FakeProvider(),
            )
        # Both close attempts ran, and the Green run is explicitly failed.
        self.assertEqual(mock_close.call_count, 2)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stop_reason, "close_incomplete")
        # task_done keeps the task verdict (done); the run-level close event
        # keeps the stream consistent with the non-zero exit.
        task_done = next(row for row in rows if row.get("type") == "task_done")
        self.assertEqual(task_done["stop_reason"], "done")
        closing = rows[-1]
        self.assertEqual(closing["type"], "headless_close")
        self.assertEqual(closing["stop_reason"], "close_incomplete")
        self.assertEqual(closing["exit_code"], 1)
        self.assertEqual(closing["run_id"], result.run_id)
        self.assertEqual(closing["session_id"], result.session_id)

    def test_headless_close_payload_is_bounded(self) -> None:
        from codey.app.headless_runner import headless_event_payload

        payload = headless_event_payload({
            "type": "headless_close",
            "run_id": "run-1",
            "session_id": "session-1",
            "stop_reason": "close_incomplete",
            "exit_code": 1,
        })
        assert payload is not None
        self.assertEqual(payload["type"], "headless_close")
        self.assertEqual(payload["stop_reason"], "close_incomplete")
        self.assertEqual(payload["exit_code"], 1)

    def test_headless_close_failure_keeps_task_exception(self) -> None:
        rows: list[dict[str, object]] = []
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch(
                "codey.app.headless_runner.run_task_submission",
                side_effect=RuntimeError("task blew up"),
            ),
            mock.patch.object(HeadlessAppContext, "close", return_value=False),
            self.assertRaises(RuntimeError) as cm,
        ):
            run_headless(
                HeadlessRequest(
                    project=Path(td, "project"),
                    task="failing task",
                    provider_id="qwen",
                    run_id="run-explicit",
                    session_id="session-explicit",
                    state_home=Path(td, "state"),
                ),
                emit_jsonl=rows.append,
                connect_provider=lambda *_args, **_kwargs: _FakeProvider(),
            )
        # Cleanup failure is attached, never a replacement for the cause,
        # but the retained resources stay visible in the stream with the
        # explicit run id, never an empty id.
        self.assertIn("task blew up", str(cm.exception))
        closing = rows[-1]
        self.assertEqual(closing["type"], "headless_close")
        self.assertEqual(closing["stop_reason"], "close_incomplete")
        self.assertEqual(closing["run_id"], "run-explicit")
        self.assertEqual(closing["session_id"], "session-explicit")

    def test_headless_close_raising_keeps_task_error(self) -> None:
        rows: list[dict[str, object]] = []
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch(
                "codey.app.headless_runner.run_task_submission",
                side_effect=RuntimeError("task blew up"),
            ),
            mock.patch.object(
                HeadlessAppContext, "close", side_effect=OSError("close boom")
            ),
            self.assertRaises(RuntimeError) as cm,
        ):
            run_headless(
                HeadlessRequest(
                    project=Path(td, "project"),
                    task="failing task",
                    provider_id="qwen",
                    run_id="run-explicit",
                    session_id="session-explicit",
                    state_home=Path(td, "state"),
                ),
                emit_jsonl=rows.append,
                connect_provider=lambda *_args, **_kwargs: _FakeProvider(),
            )
        self.assertIn("task blew up", str(cm.exception))
        self.assertNotIn("close boom", str(cm.exception))
        closing = rows[-1]
        self.assertEqual(closing["type"], "headless_close")
        self.assertEqual(closing["run_id"], "run-explicit")

    def test_headless_close_failure_keeps_failed_task_result(self) -> None:
        from codey.agents.runner import RunResult as _RunResult

        rows: list[dict[str, object]] = []
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(HeadlessAppContext, "close", return_value=False),
        ):
            result = run_headless(
                HeadlessRequest(
                    project=Path(td, "project"),
                    task="failing task",
                    provider_id="qwen",
                    max_turns=1,
                    session_id="session-task-fail",
                    state_home=Path(td, "state"),
                ),
                emit_jsonl=rows.append,
                agent_run=lambda *_args, **_kwargs: _RunResult("need approval", "approval", 1),
                collect_changes=lambda *_args, **_kwargs: {"ok": True, "changed_count": 0, "files": [], "diff": ""},
                connect_provider=lambda *_args, **_kwargs: _FakeProvider(),
            )
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stop_reason, "approval")
        closing = rows[-1]
        self.assertEqual(closing["type"], "headless_close")
        self.assertEqual(closing["stop_reason"], "close_incomplete")
        self.assertEqual(closing["run_id"], result.run_id)


if __name__ == "__main__":
    unittest.main()
