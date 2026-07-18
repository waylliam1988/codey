from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey import agent
from codey import server
from codey.agent import RunResult
from codey.events import RunEvent
from codey.execution_evidence import CheckEvidence
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
    def setUp(self) -> None:
        self.consensus_patch = mock.patch.object(server, "_run_consensus", return_value=None)
        self.consensus_patch.start()

    def tearDown(self) -> None:
        self.consensus_patch.stop()

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

    def test_missing_successful_changes_field_keeps_existing_commands(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            store = ProjectFactsStore(state_td)
            path = store.path_for(td)
            path.parent.mkdir(parents=True)
            path.write_text(
                '{"schema_version":1,"commands":[{"command":"python -m unittest","cwd":".","kind":"check"}]}',
                encoding="utf-8",
            )

            facts = store.load(td)

        self.assertEqual([item.command for item in facts.commands], ["python -m unittest"])
        self.assertEqual(facts.successful_changes, ())

    def test_records_successful_change_only_from_verified_local_checks(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            store = ProjectFactsStore(state_td)

            self.assertFalse(store.record_successful_change(
                td,
                task="Implement feature",
                files=["app.py"],
                check_commands=["python app.py"],
            ))
            self.assertTrue(store.record_successful_change(
                td,
                task="Implement feature",
                files=["app.py", "../secret.py", "tests/test_app.py"],
                check_commands=["python -m unittest", "python app.py"],
                receipt="2 files changed · checks passed",
            ))

            rendered = store.render(td)
            facts = store.load(td)

        self.assertEqual(len(facts.successful_changes), 1)
        self.assertIn("successful change: Implement feature", rendered)
        self.assertIn("files: app.py, tests/test_app.py", rendered)
        self.assertIn("checks: python -m unittest", rendered)
        self.assertNotIn("../secret.py", rendered)

    def test_successful_change_keeps_check_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            store = ProjectFactsStore(state_td)

            self.assertFalse(store.record_successful_change(
                td,
                task="Implement backend feature",
                files=["backend/app.js"],
                checks=[CheckEvidence("pnpm test", "../outside")],
            ))
            self.assertTrue(store.record_successful_change(
                td,
                task="Implement backend feature",
                files=["backend/app.js"],
                checks=[
                    CheckEvidence("pnpm test", "backend"),
                    CheckEvidence("python app.py", "backend"),
                ],
            ))

            facts = store.load(td)
            rendered = store.render(td)

        self.assertEqual(len(facts.successful_changes), 1)
        self.assertEqual(facts.successful_changes[0].checks[0].command, "pnpm test")
        self.assertEqual(facts.successful_changes[0].checks[0].cwd, "backend")
        self.assertIn("checks: backend/: pnpm test", rendered)
        self.assertNotIn("python app.py", rendered)

    def test_successful_change_loads_legacy_check_strings(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            store = ProjectFactsStore(state_td)
            path = store.path_for(td)
            path.parent.mkdir(parents=True)
            path.write_text(
                '{"schema_version":1,"commands":[],"successful_changes":['
                '{"task":"Legacy task","files":["app.py"],'
                '"checks":["python -m unittest"]}]}',
                encoding="utf-8",
            )

            facts = store.load(td)
            rendered = store.render(td)

        self.assertEqual(facts.successful_changes[0].checks[0].command, "python -m unittest")
        self.assertEqual(facts.successful_changes[0].checks[0].cwd, ".")
        self.assertIn("checks: python -m unittest", rendered)

    def test_successful_change_loads_structured_checks_safely(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            store = ProjectFactsStore(state_td)
            path = store.path_for(td)
            path.parent.mkdir(parents=True)
            path.write_text(
                '{"schema_version":1,"commands":[],"successful_changes":['
                '{"task":"Structured task","files":["backend/app.js"],'
                '"checks":['
                '{"command":"pnpm test","cwd":"backend"},'
                '{"command":"python app.py","cwd":"backend"},'
                '{"command":"python -m unittest","cwd":"../outside"}'
                ']}]}',
                encoding="utf-8",
            )

            facts = store.load(td)
            rendered = store.render(td)

        self.assertEqual(len(facts.successful_changes), 1)
        self.assertEqual(len(facts.successful_changes[0].checks), 1)
        self.assertEqual(facts.successful_changes[0].checks[0].command, "pnpm test")
        self.assertEqual(facts.successful_changes[0].checks[0].cwd, "backend")
        self.assertIn("checks: backend/: pnpm test", rendered)
        self.assertNotIn("python app.py", rendered)

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

    def test_task_runner_records_successful_change_with_check_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            backend = project / "backend"
            backend.mkdir(parents=True)
            (backend / "package.json").write_text(
                '{"scripts":{"test":"node test.js"}}',
                encoding="utf-8",
            )
            (backend / "app.js").write_text("before\n", encoding="utf-8")
            state = server.State(root / "state")
            provider = mock.Mock()
            provider.name = "DeepSeek Web"

            def fake_agent_run(*args, **kwargs):
                target = Path(args[1]) / "backend" / "app.js"
                target.write_text("after\n", encoding="utf-8")
                kwargs["on_event"](RunEvent.tool_finished(
                    1,
                    ToolCall("edit", {"path": "backend/app.js"}),
                    ToolOutcome("edited", True, changed=True),
                ))
                kwargs["on_event"](RunEvent.tool_finished(
                    2,
                    ToolCall("run", {"path": "backend", "command": "npm test"}),
                    ToolOutcome("ok", True, exit_code=0),
                ))
                return RunResult("complete", "done", 2, True, True, True)

            changes = {
                "ok": True,
                "mode": "git",
                "changed_count": 1,
                "files": [{"path": "backend/app.js", "status": "M"}],
                "diff": "diff --git a/backend/app.js b/backend/app.js\n+after\n",
            }

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=fake_agent_run),
                mock.patch.object(server, "collect_changes", return_value=changes),
                mock.patch.object(server, "_run_project_audit", return_value=()),
                mock.patch.object(server, "_run_review", return_value=None),
                mock.patch(
                    "codey.verification_policy.shutil.which",
                    return_value="npm",
                ),
            ):
                server._run_task("session-1", str(project), "Update backend", 8, False, "deepseek")

            facts = state.project_facts.load(project)
            rendered = state.project_facts.render(project)

        self.assertEqual(facts.successful_changes[-1].checks[0].command, "npm test")
        self.assertEqual(facts.successful_changes[-1].checks[0].cwd, "backend")
        self.assertIn("checks: backend/: npm test", rendered)

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
