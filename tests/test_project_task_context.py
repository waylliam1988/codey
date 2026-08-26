from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path
from unittest import mock

from codey.workspace import task_context as project_task_context
from codey.knowledge import KnowledgeNote, KnowledgeStore
from codey.workspace.facts import ProjectFactsStore
from codey.workspace.task_context import ProjectTaskContext, ProjectTaskContextBuilder
from codey.runs.work_checkpoint import WorkCheckpointStore


class BrokenFacts:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def render(self, _project):
        raise self.exc

    def load(self, _project):
        raise self.exc


class BrokenCheckpointStore:
    def load(self, _session_id):
        return None

    def start(self, **_kwargs):
        raise OSError("cannot write")


class ProjectTaskContextBuilderTests(unittest.TestCase):
    def test_context_does_not_store_rendered_candidate_command_lines(self) -> None:
        self.assertNotIn(
            "verification_command_lines",
            {item.name for item in fields(ProjectTaskContext)},
        )

    def test_build_without_stores_still_returns_project_map(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("def route():\n    return True\n", encoding="utf-8")

            context = ProjectTaskContextBuilder().build(
                project=root,
                task="change route",
                session_id="s",
                run_id="r",
                continue_task=False,
                provider_session_changed=False,
            )

        self.assertIn("Project Map", context.project_map)
        self.assertEqual(context.verified_facts, "")
        self.assertIsNone(context.checkpoint.item)
        self.assertFalse(context.checkpoint.resumed)

    def test_build_starts_checkpoint_when_not_resuming(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td, "project")
            root.mkdir()
            store = WorkCheckpointStore(Path(td, "state"))

            context = ProjectTaskContextBuilder(work_checkpoints=store).build(
                project=root,
                task="new task",
                session_id="s",
                run_id="r",
                continue_task=False,
                provider_session_changed=False,
            )

        self.assertIsNotNone(context.checkpoint.item)
        self.assertFalse(context.checkpoint.resumed)
        self.assertEqual(context.checkpoint.prompt, "")
        self.assertEqual(context.checkpoint.changed_files, ())

    def test_continue_resumes_checkpoint_with_prompt_and_checks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td, "project")
            root.mkdir()
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            store = WorkCheckpointStore(Path(td, "state"))
            checkpoint = store.start(run_id="old", session_id="s", project=root, task="fix app")
            checkpoint = store.record_edit(checkpoint, "app.py")
            store.record_run(checkpoint, command="python -m pytest", cwd=".", ok=True)

            context = ProjectTaskContextBuilder(work_checkpoints=store).build(
                project=root,
                task="fix app",
                session_id="s",
                run_id="new",
                continue_task=True,
                provider_session_changed=False,
            )

        self.assertTrue(context.checkpoint.resumed)
        self.assertIn("Local execution checkpoint", context.checkpoint.prompt)
        self.assertEqual(context.checkpoint.changed_files, ("app.py",))
        self.assertEqual(context.checkpoint.seed_checks[0].command, "python -m pytest")
        self.assertEqual(context.resumed_verification_commands[0].command, "python -m pytest")
        self.assertTrue(
            any(item.command == "python -m pytest" for item in context.verification_candidates)
        )

    def test_provider_session_changed_resumes_only_same_task(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td, "project")
            root.mkdir()
            store = WorkCheckpointStore(Path(td, "state"))
            store.start(run_id="old", session_id="s", project=root, task="original")
            builder = ProjectTaskContextBuilder(work_checkpoints=store)

            same = builder.build(
                project=root,
                task="original",
                session_id="s",
                run_id="same",
                continue_task=False,
                provider_session_changed=True,
            )
            different = builder.build(
                project=root,
                task="different",
                session_id="s",
                run_id="different",
                continue_task=False,
                provider_session_changed=True,
            )

        self.assertTrue(same.checkpoint.resumed)
        self.assertFalse(different.checkpoint.resumed)
        self.assertEqual(different.checkpoint.item.original_task, "different")

    def test_workspace_drift_returns_workspace_changed_for_caller(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td, "project")
            root.mkdir()
            path = root / "app.py"
            path.write_text("VALUE = 1\n", encoding="utf-8")
            store = WorkCheckpointStore(Path(td, "state"))
            checkpoint = store.start(run_id="old", session_id="s", project=root, task="fix")
            checkpoint = store.record_edit(checkpoint, "app.py")
            store.record_run(checkpoint, command="python -m pytest", cwd=".", ok=True)
            path.write_text("VALUE = 2\n", encoding="utf-8")

            context = ProjectTaskContextBuilder(work_checkpoints=store).build(
                project=root,
                task="fix",
                session_id="s",
                run_id="new",
                continue_task=True,
                provider_session_changed=False,
            )

        self.assertTrue(context.checkpoint.workspace_changed)
        self.assertEqual(context.checkpoint.seed_checks, ())
        self.assertEqual(context.checkpoint.resumed_verification_commands, ())

    def test_checkpoint_start_failure_degrades_to_empty_checkpoint_context(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            context = ProjectTaskContextBuilder(
                work_checkpoints=BrokenCheckpointStore(),
            ).build(
                project=root,
                task="task",
                session_id="s",
                run_id="r",
                continue_task=False,
                provider_session_changed=False,
            )

        self.assertIsNone(context.checkpoint.item)
        self.assertFalse(context.checkpoint.resumed)

    def test_refresh_none_returns_empty_checkpoint_context(self) -> None:
        context = ProjectTaskContextBuilder().refresh_checkpoint(None)

        self.assertIsNone(context.item)
        self.assertEqual(context.prompt, "")

    def test_project_facts_os_error_degrades_but_type_error_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            degraded = ProjectTaskContextBuilder(project_facts=BrokenFacts(OSError("x"))).build(
                project=root,
                task="task",
                session_id="s",
                run_id="r",
                continue_task=False,
                provider_session_changed=False,
            )

            with self.assertRaises(TypeError):
                ProjectTaskContextBuilder(project_facts=BrokenFacts(TypeError("bug"))).build(
                    project=root,
                    task="task",
                    session_id="s",
                    run_id="r",
                    continue_task=False,
                    provider_session_changed=False,
                )

        self.assertEqual(degraded.verified_facts, "")

    def test_project_map_render_failure_degrades_to_empty_map(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(project_task_context, "render_project_map", side_effect=RuntimeError("boom")):
                context = ProjectTaskContextBuilder().build(
                    project=Path(td),
                    task="task",
                    session_id="s",
                    run_id="r",
                    continue_task=False,
                    provider_session_changed=False,
                )

        self.assertEqual(context.project_map, "")

    def test_project_map_uses_policy_candidate_commands_for_node_manager(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.completion.verification_policy.shutil.which",
            return_value="exe",
        ):
            root = Path(td)
            (root / "package.json").write_text(
                '{"scripts":{"test":"vitest","lint":"eslint ."}}',
                encoding="utf-8",
            )
            (root / "pnpm-lock.yaml").write_text(
                "lockfileVersion: 9\n",
                encoding="utf-8",
            )

            context = ProjectTaskContextBuilder().build(
                project=root,
                task="update app",
                session_id="s",
                run_id="r",
                continue_task=False,
                provider_session_changed=False,
            )

        self.assertTrue(
            any(item.command == "pnpm test" for item in context.verification_candidates)
        )
        self.assertIn("- pnpm test", context.project_map)
        self.assertIn("- pnpm run lint", context.project_map)
        self.assertNotIn("- npm test", context.project_map)
        self.assertNotIn("- npm run lint", context.project_map)

    def test_project_map_uses_policy_candidate_commands_for_python_tools(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.completion.verification_policy.shutil.which",
            return_value="exe",
        ):
            root = Path(td)
            (root / "pyproject.toml").write_text(
                "[tool.ruff]\n[tool.mypy]\n",
                encoding="utf-8",
            )

            context = ProjectTaskContextBuilder().build(
                project=root,
                task="update app",
                session_id="s",
                run_id="r",
                continue_task=False,
                provider_session_changed=False,
            )

        commands = {item.command for item in context.verification_candidates}
        self.assertIn("ruff check .", commands)
        self.assertIn("mypy .", commands)
        self.assertIn("- ruff check .", context.project_map)
        self.assertIn("- mypy .", context.project_map)
        self.assertNotIn("- python -m ruff check .", context.project_map)
        self.assertNotIn("- python -m mypy .", context.project_map)

    def test_project_config_commands_feed_candidates_and_project_map(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "codey.completion.verification_policy.shutil.which",
            return_value="python",
        ):
            root = Path(td)
            config = root / ".codey" / "config.json"
            config.parent.mkdir()
            config.write_text(
                json.dumps({
                    "schema_version": 1,
                    "verification": {
                        "commands": [
                            {
                                "command": "python -m unittest discover",
                                "cwd": ".",
                                "label": "project unittest",
                            }
                        ]
                    },
                }),
                encoding="utf-8",
            )

            context = ProjectTaskContextBuilder().build(
                project=root,
                task="update app",
                session_id="s",
                run_id="r",
                continue_task=False,
                provider_session_changed=False,
            )

        self.assertIn(
            "python -m unittest discover",
            {item.command for item in context.verification_candidates},
        )
        self.assertIn("- python -m unittest discover", context.project_map)

    def test_project_config_ignored_paths_feed_project_map(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / ".codey" / "config.json"
            config.parent.mkdir()
            config.write_text(
                json.dumps({
                    "schema_version": 1,
                    "scan": {"ignored_paths": ["generated/"]},
                }),
                encoding="utf-8",
            )
            (root / "generated").mkdir()
            (root / "generated" / "router.py").write_text(
                "def hidden_router():\n    pass\n",
                encoding="utf-8",
            )
            (root / "src" / "generated").mkdir(parents=True)
            (root / "src" / "generated" / "router.py").write_text(
                "def visible_router():\n    pass\n",
                encoding="utf-8",
            )

            context = ProjectTaskContextBuilder().build(
                project=root,
                task="generated router",
                session_id="s",
                run_id="r",
                continue_task=False,
                provider_session_changed=False,
            )

        self.assertNotIn("- generated/router.py", context.project_map)
        self.assertIn("src/generated/router.py", context.project_map)
        self.assertIn("visible_router", context.project_map)

    def test_project_config_warnings_are_bounded_context_facts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / ".codey" / "config.json"
            config.parent.mkdir()
            config.write_text("{bad json", encoding="utf-8")

            context = ProjectTaskContextBuilder().build(
                project=root,
                task="task",
                session_id="s",
                run_id="r",
                continue_task=False,
                provider_session_changed=False,
            )

        self.assertIn("invalid JSON", context.project_config_warnings)
        self.assertNotIn("{bad json", context.project_config_warnings)

    def test_project_config_project_map_budget_can_only_reduce_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / ".codey" / "config.json"
            config.parent.mkdir()
            config.write_text(
                json.dumps({
                    "schema_version": 1,
                    "context": {"budget_hints": {"project_map_chars": 1000}},
                }),
                encoding="utf-8",
            )
            for index in range(120):
                (root / f"long_named_project_file_{index:03}_for_budget_check.py").write_text(
                    "x = 1\n",
                    encoding="utf-8",
                )

            context = ProjectTaskContextBuilder().build(
                project=root,
                task="task",
                session_id="s",
                run_id="r",
                continue_task=False,
                provider_session_changed=False,
            )

        self.assertLessEqual(len(context.project_map), 1080)
        self.assertIn("map truncated by character budget", context.project_map)

    def test_verified_project_facts_feed_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td, "project")
            root.mkdir()
            facts = ProjectFactsStore(Path(td, "state"))
            facts.record_success(root, ".", "python -m pytest")

            context = ProjectTaskContextBuilder(project_facts=facts).build(
                project=root,
                task="task",
                session_id="s",
                run_id="r",
                continue_task=False,
                provider_session_changed=False,
            )

        self.assertEqual(context.verification_verified_commands[0].command, "python -m pytest")
        self.assertTrue(
            any(item.command == "python -m pytest" for item in context.verification_candidates)
        )

    def test_research_context_is_bounded_session_brief(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td, "project")
            root.mkdir()
            store = KnowledgeStore(Path(td, "state", "vault"))
            other = KnowledgeNote.create(
                type="synthesis",
                title="Other research",
                body="Unrelated",
                session_id="other",
            )
            current = KnowledgeNote.create(
                type="synthesis",
                title="Current research",
                body=(
                    "## 结论\n"
                    "- Use the documented API. [1 p.4]\n\n"
                    "## 关键证据\n"
                    "- [1 p.4] The API doc confirms the flow.\n\n"
                    "## 反证与限制\n"
                    "- 未找到强反证。\n\n"
                    "## 来源质量\n"
                    "- [1] primary · official · fresh · example.com\n\n"
                    "## 搜索覆盖\n"
                    "- query: documented api\n\n"
                    "## 来源\n"
                    "[1] API docs - https://example.com/api\n\n"
                    "## Evidence Ledger\n"
                    "### Evidence Items\n"
                    "- [supports] Use the documented API\n"
                    "  source: https://example.com/api p.4\n"
                    "  excerpt: The API doc confirms the flow.\n"
                ),
                sources=["https://example.com/api"],
                session_id="s",
            )
            store.write_note(other)
            store.write_note(current)

            context = ProjectTaskContextBuilder(knowledge_store=store).build(
                project=root,
                task="build",
                session_id="s",
                run_id="r",
                continue_task=False,
                provider_session_changed=False,
            )
            store.close()

        self.assertIn("Research context from this chat", context.research_context)
        self.assertIn("Use the documented API", context.research_context)
        self.assertIn("p.4", context.research_context)
        self.assertIn("Citation map", context.research_context)
        self.assertIn("Counter-evidence / limitations", context.research_context)
        self.assertNotIn("Other research", context.research_context)
        self.assertEqual(context.knowledge_context, context.research_context)


if __name__ == "__main__":
    unittest.main()
