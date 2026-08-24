from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey import server
from codey.agent import RunResult
from codey.events import RunEvent
from codey.models import ToolCall
from codey.run_ledger import (
    MAX_LEDGER_BYTES,
    RunLedgerStore,
    RunLedgerWriter,
    read_ledger,
)
from codey.run_ledger_projection import (
    build_task_receipt_from_projection,
    load_run_projection,
    receipt_from_projection_if_compatible,
)
from codey.tool_definition import TOOL_DEFINITION_BY_NAME
from codey.tool_runtime import ToolOutcome


VALID_SHA256 = "a" * 64


class RunLedgerStoreTests(unittest.TestCase):
    def test_path_for_keeps_session_and_run_inside_state_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunLedgerStore(td)
            path = store.path_for("../session", "../../run")

            self.assertEqual(path.parent.parent, Path(td) / "run_ledgers")
            self.assertNotIn("..", path.name)
            self.assertTrue(path.name.endswith(".jsonl"))

    def test_model_reply_records_size_not_reply_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunLedgerStore(td)
            writer = store.open(
                run_id="run_1",
                session_id="session-1",
                project="E:/project",
                task="Fix bug",
                provider="deepseek",
                mode="agent",
            )
            secret_reply = "SECRET_REPLY_SHOULD_NOT_BE_SAVED " * 100
            writer.append_run_event(RunEvent.turn_started(2, secret_reply, note="(done)"))

            rows = [item.payload for item in read_ledger(store.path_for("session-1", "run_1"))]
            model_reply = next(item for item in rows if item["type"] == "model_reply")
            serialized = json.dumps(rows, ensure_ascii=False)

            self.assertEqual(model_reply["reply_chars"], len(secret_reply))
            self.assertNotIn("SECRET_REPLY_SHOULD_NOT_BE_SAVED", serialized)

    def test_run_event_projects_file_change_and_verified_command(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunLedgerStore(td)
            writer = store.open(
                run_id="run_2",
                session_id="session-2",
                project="E:/project",
                task="Update app",
                provider="deepseek",
                mode="agent",
            )
            writer.append_run_event(RunEvent.tool_finished(
                1,
                ToolCall("edit", {"path": "app.py"}),
                ToolOutcome("edited app.py", True, changed=True),
                index=0,
            ))
            writer.append_run_event(RunEvent.tool_finished(
                2,
                ToolCall("run", {"path": ".", "command": "python -m pytest -q"}),
                ToolOutcome(
                    "ok",
                    True,
                    audit={
                        "managed_output": {
                            "handle": "out_0001_abc",
                            "original_bytes": 1234,
                            "stored_bytes": 1000,
                            "sha256": VALID_SHA256,
                        }
                    },
                    exit_code=0,
                ),
                index=1,
            ))

            rows = [item.payload for item in read_ledger(store.path_for("session-2", "run_2"))]
            changed = next(item for item in rows if item["type"] == "file_changed")
            verified = next(item for item in rows if item["type"] == "command_verified")
            tool_finished = next(
                item
                for item in rows
                if item["type"] == "tool_finished" and item["tool"] == "run"
            )

            self.assertIn(changed["type"], TOOL_DEFINITION_BY_NAME["edit"].output_facts)
            self.assertIn(verified["type"], TOOL_DEFINITION_BY_NAME["run"].output_facts)
            self.assertEqual(changed["path"], "app.py")
            self.assertEqual(verified["command"], "python -m pytest -q")
            self.assertEqual(verified["cwd"], ".")
            self.assertEqual(verified["tool_id"], "2:1")
            self.assertEqual(tool_finished["output_handle"], "out_0001_abc")
            self.assertEqual(tool_finished["output_bytes"], 1234)
            self.assertEqual(tool_finished["output_stored_bytes"], 1000)
            self.assertEqual(tool_finished["output_sha256"], VALID_SHA256)

    def test_run_event_ignores_invalid_managed_output_handle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunLedgerStore(td)
            writer = store.open(
                run_id="run_bad_handle",
                session_id="session-bad-handle",
                project="E:/project",
                task="Run tests",
                provider="deepseek",
                mode="agent",
            )
            writer.append_run_event(RunEvent.tool_finished(
                1,
                ToolCall("run", {"path": ".", "command": "python -m pytest -q"}),
                ToolOutcome(
                    "ok",
                    True,
                    audit={
                        "managed_output": {
                            "handle": "../x",
                            "original_bytes": 1234,
                            "stored_bytes": 1000,
                            "sha256": VALID_SHA256,
                        }
                    },
                    exit_code=0,
                ),
                index=0,
            ))

            rows = [
                item.payload
                for item in read_ledger(
                    store.path_for("session-bad-handle", "run_bad_handle")
                )
            ]
            tool_finished = next(item for item in rows if item["type"] == "tool_finished")

            self.assertNotIn("output_handle", tool_finished)
            self.assertNotIn("output_bytes", tool_finished)
            self.assertNotIn("output_stored_bytes", tool_finished)
            self.assertNotIn("output_sha256", tool_finished)

    def test_run_event_empties_invalid_managed_output_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = RunLedgerStore(td)
            writer = store.open(
                run_id="run_bad_sha",
                session_id="session-bad-sha",
                project="E:/project",
                task="Run tests",
                provider="deepseek",
                mode="agent",
            )
            writer.append_run_event(RunEvent.tool_finished(
                1,
                ToolCall("run", {"path": ".", "command": "python -m pytest -q"}),
                ToolOutcome(
                    "ok",
                    True,
                    audit={
                        "managed_output": {
                            "handle": "out_0001_valid",
                            "original_bytes": 1234,
                            "stored_bytes": 1000,
                            "sha256": "abc\nINJECTED",
                        }
                    },
                    exit_code=0,
                ),
                index=0,
            ))

            rows = [
                item.payload
                for item in read_ledger(store.path_for("session-bad-sha", "run_bad_sha"))
            ]
            serialized = json.dumps(rows, ensure_ascii=False)
            tool_finished = next(item for item in rows if item["type"] == "tool_finished")

            self.assertEqual(tool_finished["output_handle"], "out_0001_valid")
            self.assertEqual(tool_finished["output_sha256"], "")
            self.assertNotIn("INJECTED", serialized)

    def test_ledger_truncates_once_when_byte_budget_is_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.jsonl"
            writer = RunLedgerWriter(path, run_id="run_big", session_id="session-big")
            writer.bytes_written = MAX_LEDGER_BYTES - 1

            writer.append("info", text="too large")
            writer.append("info", text="ignored")

            rows = [item.payload for item in read_ledger(path)]
            self.assertEqual([item["type"] for item in rows], ["ledger_truncated"])
            self.assertTrue(writer.disabled)

    def test_reopened_writer_continues_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.jsonl"
            first = RunLedgerWriter(path, run_id="run", session_id="session")
            first.append("info", text="first")
            second = RunLedgerWriter(path, run_id="run", session_id="session")
            second.append("info", text="second")

            rows = [item.payload for item in read_ledger(path)]

        self.assertEqual([item["seq"] for item in rows], [1, 2])

    def test_append_failure_disables_writer_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            writer = RunLedgerWriter(Path(td) / "ledger.jsonl", run_id="run", session_id="session")
            with mock.patch.object(Path, "open", side_effect=OSError("no disk")):
                writer.append("info", text="fails")
                writer.append("info", text="ignored")

            self.assertTrue(writer.disabled)


class RunLedgerTaskRunnerIntegrationTests(unittest.TestCase):
    def _provider(self):
        provider = mock.Mock()
        provider.name = "DeepSeek Web"
        provider.location = "https://chat.deepseek.com/"
        return provider

    def test_state_without_state_home_does_not_enable_run_ledger_store(self) -> None:
        with mock.patch.object(server, "RunLedgerStore") as store_class:
            state = server.State()

        self.assertIsNone(state.run_ledgers)
        self.assertIsNone(state.managed_outputs)
        store_class.assert_not_called()

    def test_project_task_writes_bounded_run_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("before\n", encoding="utf-8")
            state = server.State(root / "state")
            provider = self._provider()

            def fake_agent(*args, **kwargs):
                (project / "app.py").write_text("after\n", encoding="utf-8")
                kwargs["on_event"](RunEvent.turn_started(
                    1,
                    "SECRET_MODEL_REPLY_SHOULD_NOT_BE_SAVED",
                    note="(tool)",
                ))
                kwargs["on_event"](RunEvent.tool_started(
                    1,
                    ToolCall("edit", {"path": "app.py"}),
                    "Editing app.py",
                    index=0,
                ))
                kwargs["on_event"](RunEvent.tool_finished(
                    1,
                    ToolCall("edit", {"path": "app.py"}),
                    ToolOutcome("edited app.py", True, changed=True),
                    index=0,
                ))
                kwargs["on_event"](RunEvent.tool_finished(
                    2,
                    ToolCall("run", {"path": ".", "command": "python -m pytest -q"}),
                    ToolOutcome("ok", True, exit_code=0),
                    index=0,
                ))
                return RunResult("done", "done", 2, True, True, True)

            changes = {
                "ok": True,
                "mode": "snapshot",
                "changed_count": 1,
                "files": [{"path": "app.py", "status": "M", "additions": 1, "deletions": 1}],
            }
            projected_receipt_calls = []

            def spy_projected_receipt(projection, legacy_receipt):
                projected_receipt_calls.append((projection, legacy_receipt))
                return receipt_from_projection_if_compatible(projection, legacy_receipt)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=fake_agent),
                mock.patch.object(server, "collect_changes", return_value=changes),
                mock.patch.object(server, "_run_project_audit", return_value=()),
                mock.patch.object(server, "_run_review", return_value=None),
                mock.patch(
                    "codey.task_runner.receipt_from_projection_if_compatible",
                    side_effect=spy_projected_receipt,
                ),
            ):
                server._run_task("session-ledger", str(project), "Update app.py", 8, False, "deepseek")

            run_id = state.last_terminal_event["run_id"]
            path = state.run_ledgers.path_for("session-ledger", run_id)
            rows = [item.payload for item in read_ledger(path)]
            types = [item["type"] for item in rows]
            serialized = json.dumps(rows, ensure_ascii=False)

            self.assertIn("run_started", types)
            self.assertIn("provider_selected", types)
            self.assertIn("model_reply", types)
            self.assertIn("tool_started", types)
            self.assertIn("tool_finished", types)
            self.assertIn("file_changed", types)
            self.assertIn("command_verified", types)
            self.assertIn("changes_collected", types)
            self.assertEqual(types[-1], "run_finished")
            self.assertNotIn("SECRET_MODEL_REPLY_SHOULD_NOT_BE_SAVED", serialized)
            changes_row = next(item for item in rows if item["type"] == "changes_collected")
            self.assertEqual(changes_row["changed_count"], 1)
            self.assertEqual(changes_row["checks_passed"], True)
            self.assertEqual(changes_row["receipt"]["restore_available"], True)
            projection = load_run_projection(state.run_ledgers, "session-ledger", run_id)
            self.assertIsNotNone(projection)
            projected_receipt = build_task_receipt_from_projection(projection)
            self.assertIsNotNone(projected_receipt)
            self.assertEqual(state.last_terminal_event["receipt"], projected_receipt.to_dict())
            self.assertEqual(len(projected_receipt_calls), 1)
            self.assertTrue(projected_receipt_calls[0][0].complete)

    def test_ledger_append_failure_does_not_break_task(self) -> None:
        class FailingLedger:
            def append_run_event(self, _event):
                raise OSError("ledger unavailable")

            def append_changes_collected(self, *_args, **_kwargs):
                raise OSError("ledger unavailable")

            def finish(self, **_fields):
                raise OSError("ledger unavailable")

        class FailingStore:
            def open(self, **_kwargs):
                return FailingLedger()

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("before\n", encoding="utf-8")
            state = server.State(root / "state")
            state.run_ledgers = FailingStore()
            provider = self._provider()

            def fake_agent(*args, **kwargs):
                kwargs["on_event"](RunEvent.tool_finished(
                    1,
                    ToolCall("read", {"path": "app.py"}),
                    ToolOutcome("before", True),
                ))
                return RunResult("done", "done", 1, False, False, False)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=fake_agent),
                mock.patch.object(server, "collect_changes", return_value={"ok": True, "changed_count": 0, "files": []}),
                mock.patch.object(server, "_run_project_audit", return_value=()),
            ):
                server._run_task("session-fail-open", str(project), "Read app.py", 8, False, "deepseek")

            self.assertEqual(state.last_terminal_event["stop_reason"], "done")

    def test_terminal_error_records_provider_failure_in_run_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "app.py").write_text("before\n", encoding="utf-8")
            state = server.State(root / "state")
            state.provider_failover_order = lambda: ("deepseek",)
            provider = self._provider()

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=TimeoutError("response timed out")),
                mock.patch.object(server, "_run_project_audit", return_value=()),
            ):
                server._run_task(
                    "session-error-ledger",
                    str(project),
                    "Update app.py",
                    8,
                    False,
                    "deepseek",
                )

            run_id = state.last_terminal_event["run_id"]
            path = state.run_ledgers.path_for("session-error-ledger", run_id)
            rows = [item.payload for item in read_ledger(path)]
            types = [item["type"] for item in rows]
            failure = next(item for item in rows if item["type"] == "provider_failure")

            self.assertEqual(state.last_terminal_event["stop_reason"], "error")
            self.assertIn("provider_failure", types)
            self.assertLess(types.index("provider_failure"), types.index("run_finished"))
            self.assertEqual(failure["provider"], "deepseek")
            self.assertEqual(failure["action"], "task")
            self.assertEqual(failure["kind"], "transient")
            self.assertIn("response timed out", failure["message"])


if __name__ == "__main__":
    unittest.main()
