from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey import agent
from codey import server
from codey.agent import RunResult
from codey.events import RunEvent
from codey.models import ToolCall
from codey.project_facts import MAX_VERIFIED_COMMANDS, ProjectFactsStore
from codey.tool_runtime import ToolOutcome


class FakeProvider:
    name = "Fake Provider"
    location = "fake://provider"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.sent: list[str] = []

    def new_chat(self) -> None:
        pass

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        self.sent.append(text)
        return self.reply

    def close(self) -> None:
        pass


class ProjectFactsTests(unittest.TestCase):
    def test_records_only_safe_success_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            store = ProjectFactsStore(state_td)

            self.assertTrue(store.record_success(td, ".", "python -m unittest"))
            self.assertTrue(store.record_success(td, "src", "python app.py"))
            self.assertFalse(store.record_success(td, ".", "python app.py --token secret"))
            self.assertFalse(store.record_success(td, "../outside", "python -m unittest"))

            facts = store.load(td)

        self.assertEqual([item.command for item in facts.commands], [
            "python -m unittest",
            "python app.py",
        ])
        self.assertEqual(facts.commands[0].kind, "check")
        self.assertEqual(facts.commands[1].entry_file, "app.py")

    def test_deduplicates_and_bounds_verified_commands(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            store = ProjectFactsStore(state_td)
            for index in range(MAX_VERIFIED_COMMANDS + 2):
                self.assertTrue(store.record_success(td, f"pkg{index}", "python -m unittest"))
            store.record_success(td, f"pkg{MAX_VERIFIED_COMMANDS + 1}", "python -m unittest")

            facts = store.load(td)

        self.assertEqual(len(facts.commands), MAX_VERIFIED_COMMANDS)
        self.assertEqual(facts.commands[-1].cwd, f"pkg{MAX_VERIFIED_COMMANDS + 1}")

    def test_projects_are_isolated_and_invalid_state_is_ignored(self) -> None:
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
            tempfile.TemporaryDirectory() as state_td,
        ):
            store = ProjectFactsStore(state_td)
            store.record_success(first, ".", "python -m unittest")
            store.path_for(second).parent.mkdir(parents=True)
            store.path_for(second).write_text("broken", encoding="utf-8")

            self.assertIn("python -m unittest", store.render(first))
            self.assertEqual(store.render(second), "")

    def test_deleted_entry_file_is_not_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            entry = root / "app.py"
            entry.write_text("print('ok')\n", encoding="utf-8")
            store = ProjectFactsStore(state_td)
            store.record_success(root, ".", "python app.py")

            self.assertIn("python app.py", store.render(root))
            entry.unlink()

            self.assertEqual(store.render(root), "")

    def test_agent_prompt_includes_facts_without_exposing_storage_concepts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            provider = FakeProvider('{"tool":"done","args":{"summary":"ok"}}')

            result = agent.run(
                provider,
                Path(td),
                "Fix it",
                project_facts="- successful check: python -m unittest",
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("Verified project facts", provider.sent[0])
        self.assertIn("python -m unittest", provider.sent[0])
        self.assertNotIn("facts.json", provider.sent[0])

    def test_task_runner_records_success_and_injects_it_into_next_task(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            state = server.State()
            state.project_facts = ProjectFactsStore(state_td)
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            captured_facts: list[str] = []
            run_count = 0

            def fake_agent_run(*_args, **kwargs):
                nonlocal run_count
                run_count += 1
                captured_facts.append(kwargs.get("project_facts", ""))
                if run_count == 1:
                    kwargs["on_event"](RunEvent.tool_finished(
                        1,
                        ToolCall("run", {"path": ".", "command": "python -m unittest"}),
                        ToolOutcome("exit 0", True, exit_code=0),
                    ))
                return RunResult("complete", "done", 1, True)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=fake_agent_run),
                mock.patch.object(
                    server,
                    "collect_changes",
                    return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""},
                ),
            ):
                server._run_task("session-1", td, "first", 4, False, "deepseek")
                server._run_task("session-1", td, "second", 4, False, "deepseek")

        self.assertEqual(captured_facts[0], "")
        self.assertIn("python -m unittest", captured_facts[1])

    def test_task_runner_does_not_record_failed_run(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            state = server.State()
            state.project_facts = ProjectFactsStore(state_td)
            provider = mock.Mock()
            provider.name = "Qwen Studio"

            def fake_agent_run(*_args, **kwargs):
                kwargs["on_event"](RunEvent.tool_finished(
                    1,
                    ToolCall("run", {"path": ".", "command": "python -m unittest"}),
                    ToolOutcome("exit 1", False, exit_code=1),
                ))
                return RunResult("failed", "done", 1, False)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=fake_agent_run),
                mock.patch.object(
                    server,
                    "collect_changes",
                    return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""},
                ),
            ):
                server._run_task("session-1", td, "first", 4, False, "qwen")

            self.assertEqual(state.project_facts.render(td), "")

    def test_facts_persistence_failure_does_not_fail_task(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            state = server.State(state_td)
            provider = mock.Mock()
            provider.name = "DeepSeek Web"

            def fake_agent_run(*_args, **kwargs):
                kwargs["on_event"](RunEvent.tool_finished(
                    1,
                    ToolCall("run", {"path": ".", "command": "python -m unittest"}),
                    ToolOutcome("exit 0", True, exit_code=0),
                ))
                return RunResult("complete", "done", 1, True)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=fake_agent_run),
                mock.patch.object(
                    state.project_facts,
                    "record_success",
                    side_effect=OSError("disk full"),
                ),
                mock.patch.object(
                    server,
                    "collect_changes",
                    return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""},
                ),
            ):
                server._run_task("session-1", td, "task", 4, False, "deepseek")

            self.assertEqual(state.last_stop_reason, "done")


if __name__ == "__main__":
    unittest.main()
