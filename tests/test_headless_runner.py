from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codey.agents.request import AgentRequest
from codey.agents.runner import RunResult
from codey.runtime.events import MAX_EVENT_TEXT_CHARS, RunEvent
from codey.app.headless_runner import HeadlessRequest, headless_event_payload, run_headless
from codey.runtime.operation_state import RuntimeOperationStore
from codey.runtime.session_log import RuntimeSessionLog
from codey.runtime.models import ToolCall
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
            request.on_shell_request(".", "python setup.py install")
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
        seen: dict[str, object] = {}
        rows: list[dict[str, object]] = []
        route_provider = _RouteProvider(
            '{"mode":"planning_readonly","confidence":0.91,"reason":"plan only"}'
        )

        def fake_agent(request: AgentRequest):
            seen["permission_profile"] = request.permission_profile
            seen["change_tracker"] = request.change_tracker
            return RunResult("plan only", "done", 1)

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
                connect_provider=lambda *_args, **_kwargs: _FakeProvider(),
                connect_fresh_provider=lambda *_args, **_kwargs: route_provider,
            )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(rows[0]["mode"], "planning")
        self.assertEqual(rows[-1]["mode"], "planning")
        self.assertEqual(seen["permission_profile"], "planning_readonly")
        self.assertIsNone(seen["change_tracker"])
        self.assertNotIn("Ghost", route_provider.prompt)
        self.assertTrue(route_provider.closed)

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
