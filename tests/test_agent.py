from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

from codey.runtime import cancellation
from codey.agents import context as agent_context
from codey.agents import protocol as agent_protocol
from codey.agents import runner as agent
from codey.agents.request import DEFAULT_MAX_TURNS, AgentRequest
from codey.agents.tools import AgentToolFns
from codey.completion.verification_policy import VerificationCandidate
from codey.protocols import JsonToolCodec
from codey.runtime.events import render_run_event
from codey.agents.handoff import ConversationContext, ConversationSnapshot
from codey.runs.trace import RunTraceStore
from codey.toolchain import runtime as tool_runtime
from codey.runs.work_checkpoint import (
    CheckpointCheck,
    CheckpointFile,
    WorkCheckpoint,
    render_work_checkpoint,
)


class FakeProvider:
    name = "Fake Provider"
    location = "fake://provider"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.sent: list[str] = []
        self.new_chat_calls = 0

    def new_chat(self) -> None:
        self.new_chat_calls += 1

    def send(self, _text: str, timeout: float | None = None) -> str:
        del timeout
        self.sent.append(_text)
        return self.replies.pop(0)


def run_agent(provider, project, task, **kwargs):
    return agent.run(AgentRequest(provider=provider, project=Path(project), task=task, **kwargs))

    def close(self) -> None:
        pass


def _long_checkpoint_path(index: int) -> str:
    return f"src/module_{index:02d}/" + ("p" * 90) + ".py"


class ParseReplyTests(unittest.TestCase):
    def test_parse_multiple_actions_and_control(self) -> None:
        text = '{"tool":"parallel","args":{"calls":[{"tool":"read_file","args":{"path":"app.py"}},{"tool":"list_dir","args":{"path":"."}}]}}'
        plan = agent.parse_reply(text)
        calls = plan.calls
        control = plan.control

        self.assertEqual([call.name for call in calls], ["read", "ls"])
        self.assertEqual([call.args["path"] for call in calls], ["app.py", "."])
        self.assertIsNotNone(control)
        self.assertEqual(control.kind, "continue")

    def test_ignores_non_json_blocks(self) -> None:
        text = """```python
print("hello")
```

```text
<control type="continue">need files</control>
```"""
        plan = agent.parse_reply(text)

        self.assertEqual(plan.calls, [])
        self.assertIsNone(plan.control)

    def test_old_marker_protocol_is_not_parsed(self) -> None:
        text = """```text
# === codey: read path=app.py ===
```

```text
# === codey: continue ===
need files
```"""
        plan = agent.parse_reply(text)

        self.assertEqual(plan.calls, [])
        self.assertIsNone(plan.control)

    def test_parse_search_action_uses_body_as_query(self) -> None:
        text = '{"tool":"grep","args":{"path":"src","query":"login handler"}}'
        plan = agent.parse_reply(text)
        calls = plan.calls
        control = plan.control

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "search")
        self.assertEqual(calls[0].args["path"], "src")
        self.assertEqual(calls[0].args["query"], "login handler")
        self.assertIsNotNone(control)
        self.assertEqual(control.kind, "continue")

    def test_parse_json_with_web_noise_around_object(self) -> None:
        text = """已深度思考（用时 1 秒）
{"tool":"grep","args":{"path":".","query":"LEGACY_BUG"}}
"""
        plan = agent.parse_reply(text)
        calls = plan.calls
        control = plan.control

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "search")
        self.assertEqual(calls[0].args["path"], ".")
        self.assertEqual(calls[0].args["query"], "LEGACY_BUG")
        self.assertIsNotNone(control)
        self.assertEqual(control.kind, "continue")
        self.assertEqual(control.body, "Need tool result")

    def test_parse_edit_content_action(self) -> None:
        text = json.dumps({
            "tool": "edit",
            "args": {
                "path": "app.py",
                "content": 'if value < 3 and name == "x":\n    print("ok")',
            },
        })
        plan = agent.parse_reply(text)
        calls = plan.calls
        control = plan.control

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "edit")
        self.assertEqual(calls[0].args["path"], "app.py")
        self.assertEqual(calls[0].args["content"], 'if value < 3 and name == "x":\n    print("ok")')
        self.assertIsNotNone(control)
        self.assertEqual(control.kind, "continue")

    def test_parse_edit_action(self) -> None:
        text = '{"tool":"edit","args":{"path":"app.py","old_string":"old()","new_string":"new()"}}'
        plan = agent.parse_reply(text)
        calls = plan.calls
        control = plan.control

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "edit")
        self.assertEqual(calls[0].args["path"], "app.py")
        self.assertEqual(calls[0].args["replacements"], [{"search": "old()", "replace": "new()"}])
        self.assertIsNotNone(control)
        self.assertEqual(control.kind, "continue")

    def test_parse_run_action(self) -> None:
        text = '{"tool":"run","args":{"path":".","command":"python -m unittest"}}'
        plan = agent.parse_reply(text)
        calls = plan.calls
        control = plan.control

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "run")
        self.assertEqual(calls[0].args["command"], "python -m unittest")
        self.assertIsNotNone(control)
        self.assertEqual(control.kind, "continue")

    def test_parse_shell_action(self) -> None:
        text = '{"tool":"shell","args":{"path":".","command":"git status --short"}}'
        plan = agent.parse_reply(text)
        calls = plan.calls
        control = plan.control

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "shell")
        self.assertEqual(calls[0].args["command"], "git status --short")
        self.assertIsNotNone(control)
        self.assertEqual(control.kind, "continue")


class ToolTests(unittest.TestCase):
    def test_safe_join_blocks_parent_escape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            with self.assertRaises(ValueError):
                tool_runtime.safe_join(root, "../escape.txt")

    def test_write_read_and_ls(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = tool_runtime.write_file(root, "src/app.py", "print('ok')\n")
            self.assertIn("wrote src/app.py", result.model_text)

            self.assertEqual(tool_runtime.read_file(root, "src/app.py").model_text, "print('ok')\n")
            listing = tool_runtime.list_directory(root, ".").model_text
            self.assertIn("src/", listing)
            self.assertIn("app.py", listing)

    def test_read_missing_file_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                tool_runtime.read_file(Path(td), "missing.py").model_text,
                "ERROR: not a file: missing.py",
            )

    def test_search_finds_matches_with_file_and_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text(
                "def login_handler():\n    return True\n",
                encoding="utf-8",
            )

            output = tool_runtime.search_files(root, ".", "LOGIN_HANDLER").model_text

            self.assertIn("src/app.py:1: def login_handler():", output)

    def test_search_explains_that_regex_syntax_is_literal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("def login_mask():\n", encoding="utf-8")

            outcome = tool_runtime.search_files(root, ".", "def .*mask")

        self.assertTrue(outcome.ok)
        self.assertEqual(
            outcome.model_text,
            "(no literal matches; regex is not supported)",
        )

    def test_search_reads_large_source_files_within_the_bounded_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            padding = "# padding\n" * 60_000
            (root / "large.py").write_text(
                padding + "def target_after_old_limit():\n    pass\n",
                encoding="utf-8",
            )

            outcome = tool_runtime.search_files(root, "large.py", "target_after_old_limit")

        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.truncated)
        self.assertIn("large.py:60001: def target_after_old_limit():", outcome.model_text)

    def test_search_reports_oversized_files_instead_of_clean_no_matches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "huge.py").write_text("x" * 33, encoding="utf-8")
            with mock.patch("codey.toolchain.runtime.SEARCH_MAX_FILE_BYTES", 32):
                outcome = tool_runtime.search_files(root, "huge.py", "target")

        self.assertTrue(outcome.truncated)
        self.assertIn("no literal matches", outcome.model_text)
        self.assertIn("skipped 1 file(s) larger than 32 bytes", outcome.model_text)
        self.assertIn("omitted files may contain more matches", outcome.model_text)

    def test_search_counts_non_utf8_files_toward_the_total_read_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_bytes(b"\xff" * 8)
            (root / "b.py").write_text("target\n", encoding="utf-8")
            with mock.patch("codey.toolchain.runtime.SEARCH_MAX_SCAN_BYTES", 8):
                outcome = tool_runtime.search_files(root, ".", "target")

        self.assertTrue(outcome.truncated)
        self.assertIn("no literal matches", outcome.model_text)
        self.assertIn("search scan stopped at 8 bytes read budget", outcome.model_text)
        self.assertNotIn("b.py", outcome.model_text)

    def test_search_skips_excluded_directories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "node_modules").mkdir()
            (root / "node_modules" / "lib.js").write_text("login_handler\n", encoding="utf-8")

            self.assertEqual(
                tool_runtime.search_files(root, ".", "login_handler").model_text,
                "(no literal matches; regex is not supported)",
            )

    def test_search_skips_excluded_directories_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Build").mkdir()
            (root / "Build" / "out.py").write_text("login_handler\n", encoding="utf-8")

            self.assertEqual(
                tool_runtime.search_files(root, ".", "login_handler").model_text,
                "(no literal matches; regex is not supported)",
            )

    def test_search_skips_direct_excluded_start_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "node_modules").mkdir()
            (root / "node_modules" / "lib.js").write_text("login_handler\n", encoding="utf-8")

            outcome = tool_runtime.search_files(root, "node_modules", "login_handler")

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.model_text, "(no literal matches; regex is not supported)")

    def test_search_skips_direct_excluded_start_directory_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Node_Modules").mkdir()
            (root / "Node_Modules" / "lib.js").write_text("login_handler\n", encoding="utf-8")

            outcome = tool_runtime.search_files(root, "Node_Modules", "login_handler")

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.model_text, "(no literal matches; regex is not supported)")

    def test_search_does_not_skip_project_root_named_like_excluded_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "build"
            root.mkdir()
            (root / "app.py").write_text("login_handler\n", encoding="utf-8")

            outcome = tool_runtime.search_files(root, ".", "login_handler")

        self.assertTrue(outcome.ok)
        self.assertIn("app.py:1: login_handler", outcome.model_text)

    def test_search_reports_scan_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_text("pass\n", encoding="utf-8")
            (root / "b.py").write_text("pass\n", encoding="utf-8")
            (root / "c.py").write_text("login_handler\n", encoding="utf-8")

            with mock.patch("codey.toolchain.runtime.SEARCH_MAX_SCAN_FILES", 2):
                outcome = tool_runtime.search_files(root, ".", "login_handler")

        self.assertTrue(outcome.truncated)
        self.assertIn("(no literal matches; regex is not supported)", outcome.model_text)
        self.assertIn("search scan stopped after 2 files", outcome.model_text)
        self.assertIn("file budget 2", outcome.model_text)
        self.assertIn("omitted files may contain more matches", outcome.model_text)
        self.assertNotIn("c.py:1", outcome.model_text)

    def test_search_rejects_direct_symlink_start_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target.py"
            target.write_text("login_handler\n", encoding="utf-8")
            link = root / "link.py"
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"file symlink unavailable: {exc}")

            outcome = tool_runtime.search_files(root, "link.py", "login_handler")

        self.assertFalse(outcome.ok)
        self.assertIn("symlink paths are not supported for grep", outcome.model_text)
        self.assertNotIn("target.py:1", outcome.model_text)

    def test_search_rejects_direct_symlink_start_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target_dir = root / "src"
            target_dir.mkdir()
            (target_dir / "target.py").write_text("login_handler\n", encoding="utf-8")
            link = root / "linked"
            try:
                link.symlink_to(target_dir, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")

            outcome = tool_runtime.search_files(root, "linked", "login_handler")

        self.assertFalse(outcome.ok)
        self.assertIn("symlink paths are not supported for grep", outcome.model_text)
        self.assertNotIn("target.py:1", outcome.model_text)

    def test_search_requires_query(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                tool_runtime.search_files(Path(td), ".", "   ").model_text,
                "ERROR: search query required",
            )

    def test_edit_replaces_unique_search_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("def old():\n    return 1\n", encoding="utf-8")
            blocks = [tool_runtime.EditBlock("def old():", "def new():")]

            result = tool_runtime.edit_file(root, "app.py", blocks).model_text

            self.assertEqual(result, "edited app.py (1 replacement)")
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "def new():\n    return 1\n")

    def test_edit_rejects_non_unique_search_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("same\nsame\n", encoding="utf-8")
            blocks = [tool_runtime.EditBlock("same", "other")]

            result = tool_runtime.edit_file(root, "app.py", blocks).model_text

            self.assertIn("SEARCH text matched 2 times in app.py", result)
            self.assertIn("Exact matches start at lines: 1, 2.", result)
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "same\nsame\n")

    def test_edit_rejects_missing_search_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("current\n", encoding="utf-8")
            blocks = [tool_runtime.EditBlock("missing", "replacement")]

            result = tool_runtime.edit_file(root, "app.py", blocks).model_text

            self.assertIn("SEARCH text not found in app.py", result)
            self.assertIn("Use read_file and copy exact complete lines.", result)

    def test_edit_rejects_indentation_mismatch_with_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(
                "def total():\n"
                "    # target\n"
                "    return amount * rate\n",
                encoding="utf-8",
            )
            blocks = [
                tool_runtime.EditBlock(
                    "def total():\n"
                    " # target\n"
                    " return amount * rate",
                    "def total():\n"
                    " # target\n"
                    " return (amount - discount) * rate",
                )
            ]

            outcome = tool_runtime.edit_file(root, "app.py", blocks)

            self.assertFalse(outcome.ok)
            self.assertIn("SEARCH text not found in app.py", outcome.model_text)
            self.assertIn("Current bounded context near", outcome.model_text)
            # File content remains untouched
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "def total():\n"
                "    # target\n"
                "    return amount * rate\n",
            )

    def test_run_allows_py_compile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ok.py").write_text("VALUE = 1\n", encoding="utf-8")

            output = tool_runtime.run_command(
                root,
                ".",
                "python -m py_compile ok.py",
                permission_profile="coding_writer",
            ).model_text

            self.assertIn("exit 0: python -m py_compile ok.py", output)

    def test_run_allows_python_no_bytecode_flag_before_module(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "test_sample.py").write_text(
                "import unittest\n\n"
                "class SampleTests(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )

            output = tool_runtime.run_command(
                root,
                ".",
                "python -B -m unittest",
                permission_profile="coding_writer",
            ).model_text

            self.assertIn("exit 0: python -B -m unittest", output)

    def test_run_rejects_dangerous_shell_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = tool_runtime.run_command(
                Path(td),
                ".",
                "python -m unittest && rm -rf .",
                permission_profile="coding_writer",
            ).model_text

            self.assertEqual(output, "ERROR: command not allowed: python -m unittest && rm -rf .")

    def test_run_rejects_non_allowlisted_command(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = tool_runtime.run_command(
                Path(td),
                ".",
                "git status",
                permission_profile="coding_writer",
            ).model_text

            self.assertEqual(output, "ERROR: command not allowed: git status")

    def test_run_does_not_leave_python_bytecode_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "test_sample.py").write_text(
                "import unittest\n\n"
                "class SampleTests(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )

            output = tool_runtime.run_command(
                root,
                ".",
                "python -m unittest",
                permission_profile="coding_writer",
            ).model_text

            self.assertIn("exit 0: python -m unittest", output)
            self.assertFalse((root / "__pycache__").exists())


class ProjectInstructionTests(unittest.TestCase):
    def test_missing_project_instructions_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(agent_context.load_project_instructions(Path(td)), [])
            self.assertEqual(agent_context.format_project_instructions([]), "")

    def test_loads_root_agent_and_claude_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "AGENTS.md").write_text("Use tests first.\n", encoding="utf-8")
            (root / "CLAUDE.md").write_text("Keep changes small.\n", encoding="utf-8")

            docs = agent_context.load_project_instructions(root)

            self.assertEqual([doc.name for doc in docs], ["AGENTS.md", "CLAUDE.md"])
            self.assertEqual(docs[0].content, "Use tests first.\n")
            self.assertFalse(docs[0].truncated)
            formatted = agent_context.format_project_instructions(docs)
            self.assertIn("--- AGENTS.md ---", formatted)
            self.assertIn("Keep changes small.", formatted)

    def test_truncates_large_project_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "AGENTS.md").write_text("abcdef", encoding="utf-8")

            docs = agent_context.load_project_instructions(root, max_chars=3)

            self.assertEqual(len(docs), 1)
            self.assertTrue(docs[0].truncated)
            self.assertEqual(docs[0].content, "abc\n\n[content truncated]")


class DefaultsTests(unittest.TestCase):
    def test_default_turn_limit_is_shared_with_server(self) -> None:
        from codey.app import api as app_api

        self.assertEqual(DEFAULT_MAX_TURNS, 50)
        self.assertIs(app_api.DEFAULT_MAX_TURNS, DEFAULT_MAX_TURNS)


class RunLoopTests(unittest.TestCase):
    def test_intro_omits_missing_instructions_and_absolute_project_path(self) -> None:
        provider = FakeProvider('{"tool":"done","args":{"summary":"ok"}}')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            run_agent(
                provider,
                root,
                "inspect this project",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        prompt = provider.sent[0]
        self.assertIn(
            "Project workspace: use paths relative to the project root.",
            prompt,
        )
        self.assertNotIn("Project root:", prompt)
        self.assertNotIn("Project instructions:", prompt)
        self.assertNotIn("no AGENTS.md or CLAUDE.md found", prompt)
        self.assertNotIn(str(root), prompt)

    def test_intro_includes_project_instructions_when_present(self) -> None:
        provider = FakeProvider('{"tool":"done","args":{"summary":"ok"}}')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "AGENTS.md").write_text("Use tests first.\n", encoding="utf-8")

            run_agent(
                provider,
                root,
                "update this project",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        prompt = provider.sent[0]
        self.assertIn("Project instructions:", prompt)
        self.assertIn("--- AGENTS.md ---", prompt)
        self.assertIn("Use tests first.", prompt)

    def test_project_instructions_source_budget_preserves_legacy_two_file_cap(
        self,
    ) -> None:
        provider = FakeProvider('{"tool":"done","args":{"summary":"ok"}}')
        agents_tail = "AGENTS_TAIL_RULE"
        claude_head = "CLAUDE_HEAD_RULE"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "AGENTS.md").write_text(
                ("A" * 9000) + agents_tail,
                encoding="utf-8",
            )
            (root / "CLAUDE.md").write_text(
                claude_head + ("C" * 9000),
                encoding="utf-8",
            )

            run_agent(
                provider,
                root,
                "update this project",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        prompt = provider.sent[0]
        self.assertIn("--- AGENTS.md ---", prompt)
        self.assertIn("--- CLAUDE.md ---", prompt)
        self.assertIn(agents_tail, prompt)
        self.assertIn(claude_head, prompt)
        self.assertNotIn("middle of output omitted", prompt)

    def test_read_only_project_discussion_returns_direct_answer_without_changes(self) -> None:
        answer = "Start with one guided breathing rhythm.\n\nAdd customization later."
        provider = FakeProvider(json.dumps({
            "tool": "done",
            "args": {"summary": answer},
        }))

        with tempfile.TemporaryDirectory() as td:
            result = run_agent(
                provider,
                Path(td),
                "Discuss the product first. Do not write code.",
                on_event=lambda _event: None,
            )

        self.assertEqual(result.summary, answer)
        self.assertEqual(result.stop_reason, "done")
        self.assertFalse(result.changed)

    def test_project_intro_includes_project_map_when_provided(self) -> None:
        provider = FakeProvider('{"tool":"done","args":{"summary":"ok"}}')

        with tempfile.TemporaryDirectory() as td:
            result = run_agent(
                provider,
                Path(td),
                "Review project structure",
                on_event=lambda _event: None,
                project_map="Project Map (bounded local scan; relative paths only):\nManifests:\n- package.json",
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("Project Map", provider.sent[0])
        self.assertIn("package.json", provider.sent[0])

    def test_project_intro_includes_config_warnings_when_provided(self) -> None:
        provider = FakeProvider('{"tool":"done","args":{"summary":"ok"}}')

        with tempfile.TemporaryDirectory() as td:
            result = run_agent(
                provider,
                Path(td),
                "Review project config",
                on_event=lambda _event: None,
                project_config_warnings="- ignored .codey/config.json because it is invalid JSON",
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("Project config warnings:", provider.sent[0])
        self.assertIn("invalid JSON", provider.sent[0])

    def test_protocol_repair_prompt_omits_project_config_warnings(self) -> None:
        provider = FakeProvider(
            "not json",
            '{"tool":"done","args":{"summary":"ok"}}',
        )

        with tempfile.TemporaryDirectory() as td:
            result = run_agent(
                provider,
                Path(td),
                "Review project config",
                on_event=lambda _event: None,
                project_config_warnings="- invalid config warning",
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("Project config warnings:", provider.sent[0])
        self.assertNotIn("Project config warnings:", provider.sent[1])
        self.assertNotIn("invalid config warning", provider.sent[1])

    def test_project_intro_includes_research_context_when_provided(self) -> None:
        provider = FakeProvider('{"tool":"done","args":{"summary":"ok"}}')

        with tempfile.TemporaryDirectory() as td:
            result = run_agent(
                provider,
                Path(td),
                "Implement the researched tool",
                on_event=lambda _event: None,
                research_context=(
                    "Research context from this chat:\n"
                    "- synthesis_id: synthesis-1\n"
                    "Use this as background only."
                ),
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("Research context from this chat", provider.sent[0])
        self.assertIn("synthesis-1", provider.sent[0])

    def test_planning_readonly_intro_includes_ghost_directive(self) -> None:
        provider = FakeProvider('{"tool":"done","args":{"summary":"ok"}}')

        with tempfile.TemporaryDirectory() as td:
            result = run_agent(
                provider,
                Path(td),
                "Plan the change",
                on_event=lambda _event: None,
                fresh_chat=False,
                permission_profile="planning_readonly",
                ghost_directive="Local Context:\n- Prefer: concise answer-first replies.",
                ghost_continuity="Local Context:\n- Recent focus: bounded projection.",
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("Local Context:", provider.sent[0])
        self.assertIn("concise answer-first", provider.sent[0])
        self.assertIn("bounded projection", provider.sent[0])
        self.assertNotIn("Ghost", provider.sent[0])

    def test_coding_writer_intro_omits_ghost_directive(self) -> None:
        provider = FakeProvider('{"tool":"done","args":{"summary":"ok"}}')

        with tempfile.TemporaryDirectory() as td:
            result = run_agent(
                provider,
                Path(td),
                "Implement the change",
                on_event=lambda _event: None,
                fresh_chat=False,
                ghost_directive="Local Context:\n- Prefer: concise answer-first replies.",
                ghost_continuity="Local Context:\n- Recent focus: bounded projection.",
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertNotIn("Local Context:", provider.sent[0])
        self.assertNotIn("Ghost", provider.sent[0])
        self.assertNotIn("concise answer-first", provider.sent[0])
        self.assertNotIn("bounded projection", provider.sent[0])

    def test_protocol_repair_prompt_omits_ghost_directive(self) -> None:
        provider = FakeProvider(
            "not json",
            '{"tool":"done","args":{"summary":"ok"}}',
        )

        with tempfile.TemporaryDirectory() as td:
            result = run_agent(
                provider,
                Path(td),
                "Plan the change",
                on_event=lambda _event: None,
                fresh_chat=False,
                permission_profile="planning_readonly",
                ghost_directive="Local Context:\n- Prefer: concise answer-first replies.",
                ghost_continuity="Local Context:\n- Recent focus: bounded projection.",
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("Local Context:", provider.sent[0])
        self.assertNotIn("Local Context:", provider.sent[1])
        self.assertNotIn("Ghost", provider.sent[1])
        self.assertNotIn("concise answer-first", provider.sent[1])
        self.assertNotIn("bounded projection", provider.sent[1])

    def test_project_intro_includes_local_execution_checkpoint_when_provided(self) -> None:
        provider = FakeProvider('{"tool":"done","args":{"summary":"resumed"}}')
        with tempfile.TemporaryDirectory() as td:
            result = run_agent(
                provider,
                Path(td),
                "Continue the task",
                work_checkpoint=(
                    "Local execution checkpoint (bounded local facts):\n"
                    "- Recorded changed files: src/app.py\n"
                    "- Successful checks after the latest recorded change: (none)"
                ),
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("Local execution checkpoint", provider.sent[0])
        self.assertIn("src/app.py", provider.sent[0])

    def test_work_checkpoint_source_budget_preserves_changed_files_list(self) -> None:
        provider = FakeProvider('{"tool":"done","args":{"summary":"resumed"}}')
        files = tuple(
            CheckpointFile(_long_checkpoint_path(index), "sha256:" + ("0" * 64))
            for index in range(32)
        )
        checks = tuple(
            CheckpointCheck(
                "python -m pytest " + ("x" * 260),
                ".",
                1,
                "sha256:" + ("1" * 64),
            )
            for _index in range(8)
        )
        checkpoint = WorkCheckpoint(
            run_id="r",
            session_id="s",
            project="E:/project",
            original_task="T" * 1000,
            status="interrupted",
            changed_files=files,
            successful_checks_after_last_change=checks,
        )
        rendered_checkpoint = render_work_checkpoint(checkpoint)
        self.assertGreater(len(rendered_checkpoint), 3000)

        with tempfile.TemporaryDirectory() as td:
            result = run_agent(
                provider,
                Path(td),
                "Continue the task",
                work_checkpoint=rendered_checkpoint,
            )

        self.assertEqual(result.stop_reason, "done")
        prompt = provider.sent[0]
        self.assertNotIn("middle of output omitted", prompt)
        self.assertIn(files[0].path, prompt)
        self.assertIn(files[len(files) // 2].path, prompt)
        self.assertIn(files[-1].path, prompt)

    def test_initial_listing_failure_omits_listing_and_still_sends_prompt(self) -> None:
        provider = FakeProvider('{"tool":"done","args":{"summary":"ok"}}')

        def broken_listing(_root: Path, _path: str) -> tool_runtime.ToolOutcome:
            raise OSError("listing unavailable")

        with tempfile.TemporaryDirectory() as td:
            result = run_agent(
                provider,
                Path(td),
                "Continue without listing",
                on_event=lambda _event: None,
                fresh_chat=False,
                tool_fns=AgentToolFns(list_directory=broken_listing),
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(len(provider.sent), 1)
        self.assertIn("Project workspace: use paths relative to the project root.", provider.sent[0])
        self.assertIn("User task:\nContinue without listing", provider.sent[0])
        self.assertNotIn("Initial listing:", provider.sent[0])
        self.assertNotIn("why_included", provider.sent[0])
        self.assertNotIn("freshness", provider.sent[0])

    def test_initial_listing_cancel_is_not_swallowed(self) -> None:
        provider = FakeProvider('{"tool":"done","args":{"summary":"unreachable"}}')

        def stopped_listing(_root: Path, _path: str) -> tool_runtime.ToolOutcome:
            raise cancellation.TaskCancelled("task stopped")

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(cancellation.TaskCancelled):
                run_agent(
                    provider,
                    Path(td),
                    "Stop during context assembly",
                    on_event=lambda _event: None,
                    fresh_chat=False,
                    tool_fns=AgentToolFns(list_directory=stopped_listing),
                )

        self.assertEqual(provider.sent, [])

    def test_emits_structured_turn_and_tool_events(self) -> None:
        provider = FakeProvider(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"done","args":{"summary":"finished"}}',
        )
        events = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Read app.py",
                on_event=events.append,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertFalse(result.changed)
        run_events = [
            event
            for event in events
            if event.kind in {"turn", "tool_start", "tool"}
        ]
        self.assertEqual(
            [event.kind for event in run_events],
            ["turn", "tool_start", "tool", "turn"],
        )
        self.assertEqual(run_events[1].call.name, "read")
        self.assertEqual(run_events[1].message, "Reading app.py")
        self.assertEqual(run_events[1].metadata["tool_index"], 0)
        self.assertEqual(run_events[2].call.name, "read")
        self.assertEqual(run_events[2].metadata["tool_index"], 0)
        self.assertTrue(run_events[2].outcome.ok)
        self.assertEqual(run_events[2].outcome.model_text, "VALUE = 1\n")

    def test_references_tool_returns_navigation_before_done(self) -> None:
        provider = FakeProvider(
            '{"tool":"find_references","args":{"symbol":"calculate_total","path":"."}}',
            '{"tool":"done","args":{"summary":"references checked"}}',
        )
        events = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "pricing.py").write_text(
                "def calculate_total(amount):\n    return amount\n",
                encoding="utf-8",
            )
            (root / "checkout.py").write_text(
                "from pricing import calculate_total\n"
                "total = calculate_total(10)\n",
                encoding="utf-8",
            )

            result = run_agent(
                provider,
                root,
                "Find calculate_total references",
                on_event=events.append,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertFalse(result.changed)
        tool_events = [event for event in events if event.kind == "tool"]
        self.assertEqual(tool_events[0].call.name, "references")
        self.assertIn("definition pricing.py:1", tool_events[0].outcome.model_text)
        self.assertIn("[tool_result tool=find_references path=.]", provider.sent[1])
        self.assertIn("lexical scan, not semantic resolution", provider.sent[1])

    def test_coding_context_lists_files_read_after_tool_result(self) -> None:
        provider = FakeProvider(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"done","args":{"summary":"read app.py"}}',
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Read app.py",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("[tool_result tool=read_file path=app.py]", provider.sent[1])
        self.assertIn("Coding current local context:", provider.sent[1])
        self.assertIn("- Files read this run: app.py", provider.sent[1])
        self.assertIn("- Existing files eligible for exact edit: app.py", provider.sent[1])
        self.assertNotIn("Changed files needing verification", provider.sent[1])

    def test_coding_context_lists_changed_files_and_selected_verification(self) -> None:
        provider = FakeProvider(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"edit","args":{"path":"app.py","old_string":"VALUE = 1","new_string":"VALUE = 2"}}',
            '{"tool":"run","args":{"command":"python -m py_compile app.py","path":"."}}',
            '{"tool":"done","args":{"summary":"updated and checked"}}',
        )
        candidate = VerificationCandidate("python -m py_compile app.py")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Update app.py",
                on_event=lambda _event: None,
                fresh_chat=False,
                verification_candidates=(candidate,),
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertTrue(result.checks_passed)
        self.assertIn("Changed files needing verification: app.py", provider.sent[2])
        self.assertIn(
            '{"tool":"run","args":{"command":"python -m py_compile app.py","path":"."}}',
            provider.sent[2],
        )
        self.assertIn("not yet passed after the latest edit", provider.sent[2])
        self.assertIn("Changed files covered by verification: app.py", provider.sent[3])
        self.assertIn(
            "Verification covering current changes: python -m py_compile app.py (path: .)",
            provider.sent[3],
        )
        self.assertNotIn("Suggested verification for current changes", provider.sent[3])
        self.assertIn("passed after the latest edit", provider.sent[3])

    def test_coding_context_can_be_disabled_for_manual_baselines(self) -> None:
        provider = FakeProvider(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"done","args":{"summary":"read app.py"}}',
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            run_agent(
                provider,
                root,
                "Read app.py",
                on_event=lambda _event: None,
                fresh_chat=False,
                coding_context_enabled=False,
            )

        self.assertNotIn("Coding current local context:", provider.sent[1])

    def test_planning_readonly_profile_can_read_and_finish(self) -> None:
        provider = FakeProvider(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"done","args":{"summary":"read app.py"}}',
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Read app.py",
                on_event=lambda _event: None,
                fresh_chat=False,
                permission_profile="planning_readonly",
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(result.summary, "read app.py")
        self.assertIn('{"tool":"read_file"', provider.sent[0])
        self.assertNotIn('{"tool":"edit"', provider.sent[0])
        self.assertNotIn('{"tool":"run"', provider.sent[0])
        self.assertNotIn('{"tool":"shell"', provider.sent[0])

    def test_planning_readonly_profile_does_not_execute_disallowed_tools(self) -> None:
        provider = FakeProvider(
            '{"tool":"edit","args":{"path":"app.py","content":"changed\\n"}}',
            '{"tool":"done","args":{"summary":"stopped"}}',
        )

        def fail_edit(*_args, **_kwargs):
            raise AssertionError("edit should not execute")

        def fail_run(*_args, **_kwargs):
            raise AssertionError("run should not execute")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("original\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Inspect only",
                on_event=lambda _event: None,
                fresh_chat=False,
                permission_profile="planning_readonly",
                tool_fns=AgentToolFns(edit_file=fail_edit, run_command=fail_run),
                on_shell_request=lambda _request: (_ for _ in ()).throw(
                    AssertionError("shell should not execute")
                ),
            )
            content = (root / "app.py").read_text(encoding="utf-8")

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("Protocol error: disallowed tool", provider.sent[1])
        self.assertNotIn('"tool":"edit"', provider.sent[1])
        self.assertEqual(content, "original\n")

    def test_explicit_codec_is_respected_while_profile_filters_context(self) -> None:
        provider = FakeProvider(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"done","args":{"summary":"read app.py"}}',
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Read app.py",
                codec=JsonToolCodec(permission_profile="planning_readonly"),
                permission_profile="chat",
                project_facts="Fact that should be filtered",
                project_map="Map that should be filtered",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn('{"tool":"read_file"', provider.sent[0])
        self.assertNotIn("Fact that should be filtered", provider.sent[0])
        self.assertNotIn("Map that should be filtered", provider.sent[0])
        self.assertNotIn("Coding current local context:", provider.sent[1])

    def test_protocol_repair_prompt_does_not_include_coding_context(self) -> None:
        provider = FakeProvider(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"write_file","args":{"path":"new.txt","content":"hello\\n"}}',
            '{"tool":"done","args":{"summary":"stopped"}}',
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Read then create new.txt",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("Coding current local context:", provider.sent[1])
        self.assertIn("Protocol error:", provider.sent[2])
        self.assertNotIn("Coding current local context:", provider.sent[2])

    def test_invalid_parallel_batch_executes_nothing_and_requests_correction(self) -> None:
        invalid = json.dumps({
            "tool": "parallel",
            "args": {"calls": [
                {"tool": "read_file", "args": {"path": "safe.py"}},
                {
                    "tool": "edit",
                    "args": {"path": "app.py", "content": "changed\n"},
                },
            ]},
        })
        done = '{"tool":"done","args":{"summary":"stopped unsafe batch"}}'
        provider = FakeProvider(invalid, done)
        events = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "safe.py").write_text("SAFE = True\n", encoding="utf-8")
            (root / "app.py").write_text("original\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Inspect safely",
                on_event=events.append,
                fresh_chat=False,
            )
            content = (root / "app.py").read_text(encoding="utf-8")

        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(content, "original\n")
        self.assertFalse(any(event.kind == "tool" for event in events))
        self.assertIn("Protocol error:", provider.sent[1])
        self.assertIn("read-only", provider.sent[1])

    def test_unknown_write_file_tool_requests_correction_before_writing(self) -> None:
        invalid_write = json.dumps({
            "tool": "write_file",
            "args": {"path": "app.py", "content": "bad\n"},
        })
        corrected_edit = json.dumps({
            "tool": "edit",
            "args": {"path": "app.py", "content": "good\n"},
        })
        done = '{"tool":"done","args":{"summary":"created app.py"}}'
        provider = FakeProvider(invalid_write, corrected_edit, done)
        events = []

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_agent(
                provider,
                root,
                "Create app.py",
                on_event=events.append,
                fresh_chat=False,
            )

            content = (root / "app.py").read_text(encoding="utf-8")

        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(content, "good\n")
        tool_events = [event for event in events if event.kind == "tool"]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0].call.name, "edit")
        self.assertIn("Protocol error:", provider.sent[1])
        self.assertIn("unknown tool: write_file", provider.sent[1])
        self.assertIn("Preserve the previous intended path", provider.sent[1])
        self.assertIn("do not replace valid previous values", provider.sent[1])
        self.assertIn("Coding has no write_file tool", provider.sent[1])
        self.assertIn("Create a new file with edit(content=...)", provider.sent[1])
        self.assertIn(
            '{"tool":"edit","args":{"path":"app.py","content":"bad\\n"}}',
            provider.sent[1],
        )

    def test_unknown_tool_repair_uses_offending_object_not_first_valid_json(self) -> None:
        invalid = (
            '{"tool":"read_file","args":{"path":"first.py"}}\n'
            '{"tool":"write_file","args":{"path":"second.txt","content":"hello\\n"}}'
        )
        done = '{"tool":"done","args":{"summary":"stopped after repair prompt"}}'
        provider = FakeProvider(invalid, done)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "first.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Create second.txt.",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("unknown tool: write_file", provider.sent[1])
        self.assertIn(
            '{"tool":"edit","args":{"path":"second.txt","content":"hello\\n"}}',
            provider.sent[1],
        )
        self.assertNotIn('"path":"first.py","content"', provider.sent[1])

    def test_nested_tool_call_inside_done_requests_correction(self) -> None:
        nested_run_done = json.dumps({
            "tool": "done",
            "args": {
                "summary": json.dumps({
                    "tool": "run",
                    "args": {"command": "python -m unittest", "path": "."},
                }),
            },
        })
        real_run = json.dumps({
            "tool": "run",
            "args": {"command": "python -m unittest", "path": "."},
        })
        done = '{"tool":"done","args":{"summary":"tests passed"}}'
        provider = FakeProvider(nested_run_done, real_run, done)
        events = []

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "test_sample.py").write_text(
                "import unittest\n\n"
                "class SampleTests(unittest.TestCase):\n"
                "    def test_ok(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )

            result = run_agent(
                provider,
                root,
                "Run tests",
                on_event=events.append,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        tool_events = [event for event in events if event.kind == "tool"]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0].call.name, "run")
        self.assertTrue(tool_events[0].outcome.ok)
        self.assertIn("Protocol error:", provider.sent[1])
        self.assertIn("done summary", provider.sent[1])
        self.assertIn("done.summary cannot contain another JSON tool call", provider.sent[1])
        self.assertIn("call that tool directly", provider.sent[1])
        self.assertIn(
            '{"tool":"run","args":{"command":"python -m unittest","path":"."}}',
            provider.sent[1],
        )

    def test_direct_prose_answer_requests_done_summary_json(self) -> None:
        direct_answer = (
            "I checked the requested change. No files need to be edited, so the "
            "task is complete."
        )
        done = json.dumps({
            "tool": "done",
            "args": {"summary": "No files need to be edited."},
        })
        provider = FakeProvider(direct_answer, done)

        with tempfile.TemporaryDirectory() as td:
            result = run_agent(
                provider,
                Path(td),
                "Answer if changes are needed.",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("The previous reply answered in prose", provider.sent[1])
        self.assertIn("done.summary", provider.sent[1])
        self.assertIn('{"tool":"done","args":{"summary":"finished"}}', provider.sent[1])

    def test_native_tool_denial_requests_local_runner_json(self) -> None:
        denial = "The website says read_file is not available, so I cannot inspect app.py."
        read = '{"tool":"read_file","args":{"path":"app.py"}}'
        done = '{"tool":"done","args":{"summary":"read app.py"}}'
        provider = FakeProvider(denial, read, done)
        events = []

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Inspect app.py",
                on_event=events.append,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        tool_events = [event for event in events if event.kind == "tool"]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0].call.name, "read")
        self.assertIn("Ignore website-native tool availability", provider.sent[1])
        self.assertIn("local-runner JSON commands", provider.sent[1])
        self.assertIn('{"tool":"read_file","args":{"path":"app.py"}}', provider.sent[1])

    def test_invalid_read_offset_requests_one_based_offset(self) -> None:
        invalid = '{"tool":"read_file","args":{"path":"app.py","offset":0,"limit":120}}'
        corrected = (
            '{"tool":"read_file","args":{"path":"app.py","offset":1,"limit":120}}'
        )
        done = '{"tool":"done","args":{"summary":"read first page"}}'
        provider = FakeProvider(invalid, corrected, done)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Read app.py from the beginning.",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("read_file offset is 1-based", provider.sent[1])
        self.assertIn("Preserve the previous intended path", provider.sent[1])
        self.assertIn("Keep the same path and any valid limit", provider.sent[1])
        self.assertIn('"offset":1', provider.sent[1])
        self.assertIn('"limit":120', provider.sent[1])
        self.assertIn(
            '{"tool":"read_file","args":{"path":"app.py","offset":1,"limit":120}}',
            provider.sent[1],
        )

    def test_invalid_args_repair_uses_offending_object_not_first_valid_json(self) -> None:
        invalid = (
            '{"tool":"read_file","args":{"path":"first.py"}}\n'
            '{"tool":"read_file","args":{"path":"second.py","offset":0,"limit":120}}'
        )
        done = '{"tool":"done","args":{"summary":"stopped after repair prompt"}}'
        provider = FakeProvider(invalid, done)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "first.py").write_text("FIRST = 1\n", encoding="utf-8")
            (root / "second.py").write_text("SECOND = 2\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Read the second file.",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn(
            '{"tool":"read_file","args":{"path":"second.py","offset":1,"limit":120}}',
            provider.sent[1],
        )
        self.assertNotIn('"path":"first.py","offset"', provider.sent[1])

    def test_invalid_read_offset_preserves_numeric_string_limit(self) -> None:
        invalid = (
            '{"tool":"read_file","args":{"path":"app.py","offset":0,"limit":"120"}}'
        )
        done = '{"tool":"done","args":{"summary":"stopped after repair prompt"}}'
        provider = FakeProvider(invalid, done)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Read app.py from the beginning.",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn(
            '{"tool":"read_file","args":{"path":"app.py","offset":1,"limit":120}}',
            provider.sent[1],
        )

    def test_invalid_edit_mixed_modes_requests_one_edit_mode(self) -> None:
        invalid = json.dumps({
            "tool": "edit",
            "args": {
                "path": "app.py",
                "content": "VALUE = 2\n",
                "old_string": "VALUE = 1\n",
                "new_string": "VALUE = 2\n",
            },
        })
        corrected = json.dumps({
            "tool": "edit",
            "args": {
                "path": "app.py",
                "old_string": "VALUE = 1\n",
                "new_string": "VALUE = 2\n",
            },
        })
        done = '{"tool":"done","args":{"summary":"stopped after correction"}}'
        provider = FakeProvider(invalid, corrected, done)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Update app.py.",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("edit requires exactly one mode", provider.sent[1])
        self.assertIn("Preserve the previous intended path", provider.sent[1])
        self.assertIn("including escaped \\n", provider.sent[1])
        self.assertIn("old_string/new_string", provider.sent[1])
        self.assertIn(
            '{"tool":"edit","args":{"path":"app.py","old_string":"VALUE = 1\\n","new_string":"VALUE = 2\\n"}}',
            provider.sent[1],
        )

    def test_invalid_edit_mode_does_not_generate_empty_old_string_example(self) -> None:
        invalid = json.dumps({
            "tool": "edit",
            "args": {
                "path": "app.py",
                "content": "VALUE = 2\n",
                "new_string": "VALUE = 2\n",
            },
        })
        done = '{"tool":"done","args":{"summary":"stopped after repair prompt"}}'
        provider = FakeProvider(invalid, done)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Update app.py.",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertNotIn('"old_string":""', provider.sent[1])
        self.assertIn(
            '{"tool":"edit","args":{"path":"app.py","old_string":"old exact text\\n","new_string":"new text\\n"}}',
            provider.sent[1],
        )

    def test_read_file_page_arguments_reach_runtime(self) -> None:
        read = (
            '{"tool":"read_file","args":'
            '{"path":"app.py","offset":3,"limit":1}}'
        )
        done = '{"tool":"done","args":{"summary":"read page"}}'
        provider = FakeProvider(read, done)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Read the relevant page",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("three", provider.sent[1])
        self.assertNotIn("one\ntwo", provider.sent[1])
        self.assertIn("lines 3-3 of 4; next offset=4", provider.sent[1])

    def test_context_rollover_opens_fresh_chat_with_project_handoff(self) -> None:
        summary = (
            '{"goal":"Finish the original feature","decisions":["Keep app.py"],'
            '"current_state":"Tests pass"}'
        )
        done = '{"tool":"done","args":{"summary":"continued"}}'
        provider = FakeProvider(summary, done)
        recorded: list[dict[str, object]] = []

        class _CapturingTrace:
            def record_prompt_section(self, name, text, **kwargs) -> None:
                recorded.append({"name": name, "text": text, **kwargs})

            def record_context_sources(self, *_args, **_kwargs) -> None:
                return None

            def __getattr__(self, _name):
                def call(*_args, **_kwargs):
                    return None
                return call

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            context = ConversationContext(hard_limit=200)
            context.begin_window("deepseek", "project", str(root))
            context.update_snapshot(ConversationSnapshot(
                mode="project",
                goal="Finish the original feature",
                project=str(root),
                provider_id="deepseek",
                changed_files=("app.py",),
                checks_passed=True,
            ))
            context.used_tokens = 149

            result = run_agent(
                provider,
                root,
                "Add the final test",
                on_event=lambda _m: None,
                fresh_chat=False,
                conversation=context,
                provider_id="deepseek",
                trace_recorder=_CapturingTrace(),
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(provider.new_chat_calls, 1)
        self.assertIn("Return only one compact JSON object", provider.sent[0])
        self.assertIn("Factual handoff", provider.sent[1])
        self.assertIn("Finish the original feature", provider.sent[1])
        self.assertIn("Add the final test", provider.sent[1])
        handoff_rows = [
            item
            for item in recorded
            if item["name"] == "conversation_handoff_summary_prompt"
        ]
        self.assertEqual(len(handoff_rows), 1)
        handoff_row = handoff_rows[0]
        self.assertEqual(handoff_row["text"], provider.sent[0])
        self.assertEqual(handoff_row["freshness"], "provider_send")
        self.assertTrue(str(handoff_row["epoch_id"]).startswith("ctx_epoch:"))
        self.assertEqual(
            handoff_row["admission_reason"],
            "provider_turn_boundary",
        )
        self.assertEqual(handoff_row["capability_id"], "conversation_handoff")
        self.assertEqual(
            handoff_row["source_refs"],
            ("provider_send:conversation_handoff_summary",),
        )

    def test_noop_edit_does_not_require_fresh_verification(self) -> None:
        edit = (
            '{"tool":"edit","args":{"path":"app.py",'
            '"old_string":"VALUE = 1","new_string":"VALUE = 1"}}'
        )
        done = '{"tool":"done","args":{"summary":"already correct"}}'
        provider = FakeProvider(edit, done)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Check app.py and run tests if you change it.",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(result.summary, "already correct")
        self.assertFalse(result.checks_passed)
        self.assertEqual(len(provider.sent), 2)

    def test_failed_fresh_chat_does_not_reset_token_budget(self) -> None:
        summary = '{"current_state":"Keep the existing conversation"}'
        done = '{"tool":"done","args":{"summary":"continued"}}'
        provider = FakeProvider(summary, done)
        provider.new_chat = mock.Mock(side_effect=RuntimeError("button missing"))
        events = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            context = ConversationContext(hard_limit=200)
            context.begin_window("deepseek", "project", str(root))
            context.update_snapshot(ConversationSnapshot(
                mode="project",
                goal="Continue safely",
                project=str(root),
                provider_id="deepseek",
            ))
            context.used_tokens = 149

            result = run_agent(
                provider,
                root,
                "Finish the task",
                on_event=events.append,
                fresh_chat=False,
                conversation=context,
                provider_id="deepseek",
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertGreater(context.used_tokens, 149)
        self.assertTrue(any("reusing current tab" in render_run_event(event) for event in events))

    def test_strict_fresh_chat_does_not_reuse_existing_context(self) -> None:
        provider = FakeProvider('{"tool":"done","args":{"summary":"unused"}}')
        provider.new_chat = mock.Mock(side_effect=RuntimeError("button missing"))

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(RuntimeError, "button missing"):
                run_agent(
                    provider,
                    Path(td),
                    "Continue safely",
                    on_event=lambda _event: None,
                    fresh_chat=True,
                    strict_fresh_chat=True,
                )

        self.assertEqual(provider.sent, [])

    def test_failed_first_handoff_send_keeps_summary_and_budget(self) -> None:
        provider = mock.Mock()
        provider.name = "DeepSeek Web"
        provider.send.side_effect = [
            '{"current_state":"Keep this summary"}',
            TimeoutError("send failed"),
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            context = ConversationContext(hard_limit=200)
            context.begin_window("deepseek", "project", str(root))
            context.update_snapshot(ConversationSnapshot(
                mode="project",
                goal="Continue safely",
                project=str(root),
                provider_id="deepseek",
            ))
            context.used_tokens = 149

            with self.assertRaisesRegex(TimeoutError, "send failed"):
                run_agent(
                    provider,
                    root,
                    "Finish the task",
                    on_event=lambda _message: None,
                    fresh_chat=False,
                    conversation=context,
                    provider_id="deepseek",
                )

        self.assertGreater(context.used_tokens, 149)
        self.assertIn("Keep this summary", context.snapshot.conversation_summary)
        provider.new_chat.assert_called_once_with()

    def test_edit_tool_call_updates_file(self) -> None:
        read = '{"tool":"read_file","args":{"path":"app.py"}}'
        reply = '{"tool":"edit","args":{"path":"app.py","old_string":"return \'old\'","new_string":"return \'new\'"}}'
        done = '{"tool":"done","args":{"summary":"updated"}}'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("def value():\n    return 'old'\n", encoding="utf-8")

            result = run_agent(
                FakeProvider(read, reply, done),
                root,
                "update app",
                on_event=lambda _m: None,
                fresh_chat=False,
            )

            self.assertEqual(result.stop_reason, "done")
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "def value():\n    return 'new'\n")

    def test_syntax_regression_hint_reaches_next_provider_prompt(self) -> None:
        read = '{"tool":"read_file","args":{"path":"app.py"}}'
        edit = json.dumps({
            "tool": "edit",
            "args": {
                "path": "app.py",
                "old_string": "def value():",
                "new_string": "def value()",
            },
        })
        done = '{"tool":"done","args":{"summary":"syntax issue observed"}}'
        provider = FakeProvider(read, edit, done)
        events = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("def value():\n    return 1\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Change value without running checks.",
                on_event=events.append,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertTrue(result.changed)
        self.assertFalse(result.checks_passed)
        self.assertIn("Syntax regression detected in app.py", provider.sent[2])
        edit_outcomes = [
            event.outcome
            for event in events
            if event.kind == "tool"
            and event.call is not None
            and event.call.name == "edit"
        ]
        self.assertEqual(len(edit_outcomes), 1)
        self.assertTrue(edit_outcomes[0].ok)
        self.assertTrue(edit_outcomes[0].changed)

    def test_replacements_tool_call_applies_one_atomic_file_write(self) -> None:
        read = '{"tool":"read_file","args":{"path":"app.py"}}'
        edit = json.dumps({
            "tool": "edit",
            "args": {
                "path": "app.py",
                "replacements": [
                    {"old_string": "VALUE = 1", "new_string": "VALUE = 2"},
                    {"old_string": "NAME = 'old'", "new_string": "NAME = 'new'"},
                ],
            },
        })
        done = '{"tool":"done","args":{"summary":"updated"}}'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("VALUE = 1\nNAME = 'old'\n", encoding="utf-8")

            result = run_agent(
                FakeProvider(read, edit, done),
                root,
                "update both values",
                on_event=lambda _event: None,
                fresh_chat=False,
            )
            content = path.read_text(encoding="utf-8")

        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(content, "VALUE = 2\nNAME = 'new'\n")

    def test_edit_tool_call_captures_change_baseline(self) -> None:
        read = '{"tool":"read_file","args":{"path":"app.py"}}'
        reply = '{"tool":"edit","args":{"path":"app.py","old_string":"old","new_string":"new"}}'
        done = '{"tool":"done","args":{"summary":"updated"}}'

        class Tracker:
            def __init__(self) -> None:
                self.before: list[str] = []
                self.after: list[str] = []

            def capture_before(self, rel: str) -> None:
                self.before.append(rel)

            def capture_after(self, rel: str) -> None:
                self.after.append(rel)

        tracker = Tracker()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("old\n", encoding="utf-8")

            result = run_agent(
                FakeProvider(read, reply, done),
                root,
                "update app",
                on_event=lambda _m: None,
                fresh_chat=False,
                change_tracker=tracker,
            )

            self.assertEqual(result.stop_reason, "done")
            self.assertEqual(tracker.before, ["app.py"])
            self.assertEqual(tracker.after, ["app.py"])

    def test_edit_content_tool_call_writes_file(self) -> None:
        reply = '{"tool":"edit","args":{"path":"app.py","content":"VALUE = 1\\n"}}'
        done = '{"tool":"done","args":{"summary":"created"}}'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_agent(
                FakeProvider(reply, done),
                root,
                "create app",
                on_event=lambda _m: None,
                fresh_chat=False,
            )

            self.assertEqual(result.stop_reason, "done")
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "VALUE = 1\n")

    def test_existing_file_edit_requires_read_file_first(self) -> None:
        edit = '{"tool":"edit","args":{"path":"app.py","old_string":"old","new_string":"new"}}'
        read = '{"tool":"read_file","args":{"path":"app.py"}}'
        retry = '{"tool":"edit","args":{"path":"app.py","old_string":"old","new_string":"new"}}'
        done = '{"tool":"done","args":{"summary":"updated after read"}}'
        events = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")

            result = run_agent(
                FakeProvider(edit, read, retry, done),
                root,
                "update app",
                on_event=events.append,
                fresh_chat=False,
            )

            self.assertEqual(result.stop_reason, "done")
            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")

        rendered = "\n".join(render_run_event(event) for event in events)
        self.assertIn("read_file required before editing existing file: app.py", rendered)

    def test_partial_read_does_not_allow_content_overwrite_of_existing_file(self) -> None:
        read = '{"tool":"read_file","args":{"path":"large.py","offset":1,"limit":1}}'
        overwrite = '{"tool":"edit","args":{"path":"large.py","content":"lost = True\\n"}}'
        done = '{"tool":"done","args":{"summary":"not overwritten"}}'
        events = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "large.py"
            original = "first = 1\nsecond = 2\nthird = 3\n"
            path.write_text(original, encoding="utf-8")

            result = run_agent(
                FakeProvider(read, overwrite, done),
                root,
                "update large.py",
                on_event=events.append,
                fresh_chat=False,
            )

            self.assertEqual(path.read_text(encoding="utf-8"), original)

        self.assertEqual(result.stop_reason, "done")
        self.assertFalse(result.changed)
        rendered = "\n".join(render_run_event(event) for event in events)
        self.assertIn(
            "content is only allowed when creating a new file",
            rendered,
        )

    def test_blind_existing_file_edit_does_not_capture_change_baseline(self) -> None:
        edit = '{"tool":"edit","args":{"path":"app.py","old_string":"old","new_string":"new"}}'
        done = '{"tool":"done","args":{"summary":"not changed"}}'

        class Tracker:
            def __init__(self) -> None:
                self.before: list[str] = []
                self.after: list[str] = []

            def capture_before(self, rel: str) -> None:
                self.before.append(rel)

            def capture_after(self, rel: str) -> None:
                self.after.append(rel)

        tracker = Tracker()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")

            result = run_agent(
                FakeProvider(edit, done),
                root,
                "update app",
                on_event=lambda _event: None,
                fresh_chat=False,
                change_tracker=tracker,
            )

            self.assertEqual(path.read_text(encoding="utf-8"), "old\n")

        self.assertEqual(result.stop_reason, "done")
        self.assertFalse(result.changed)
        self.assertEqual(tracker.before, [])
        self.assertEqual(tracker.after, [])

    def test_failed_read_does_not_unlock_existing_file_edit(self) -> None:
        read_missing = '{"tool":"read_file","args":{"path":"missing.py"}}'
        edit = '{"tool":"edit","args":{"path":"app.py","old_string":"old","new_string":"new"}}'
        done = '{"tool":"done","args":{"summary":"not changed"}}'
        events = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")

            result = run_agent(
                FakeProvider(read_missing, edit, done),
                root,
                "update app",
                on_event=events.append,
                fresh_chat=False,
            )

            self.assertEqual(path.read_text(encoding="utf-8"), "old\n")

        self.assertEqual(result.stop_reason, "done")
        rendered = "\n".join(render_run_event(event) for event in events)
        self.assertIn("not a file: missing.py", rendered)
        self.assertIn("read_file required before editing existing file: app.py", rendered)

    def test_new_file_content_write_does_not_require_read(self) -> None:
        write = '{"tool":"edit","args":{"path":"new_app.py","content":"VALUE = 1\\n"}}'
        done = '{"tool":"done","args":{"summary":"created"}}'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_agent(
                FakeProvider(write, done),
                root,
                "create a new file",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

            self.assertEqual((root / "new_app.py").read_text(encoding="utf-8"), "VALUE = 1\n")

        self.assertEqual(result.stop_reason, "done")
        self.assertTrue(result.changed)

    def test_file_created_this_run_can_be_edited_without_reading(self) -> None:
        write = '{"tool":"edit","args":{"path":"new_app.py","content":"VALUE = 1\\n"}}'
        edit = '{"tool":"edit","args":{"path":"new_app.py","old_string":"VALUE = 1","new_string":"VALUE = 2"}}'
        done = '{"tool":"done","args":{"summary":"created and updated"}}'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = run_agent(
                FakeProvider(write, edit, done),
                root,
                "create and update a new file",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

            self.assertEqual((root / "new_app.py").read_text(encoding="utf-8"), "VALUE = 2\n")

        self.assertEqual(result.stop_reason, "done")
        self.assertTrue(result.changed)

    def test_read_files_unlocks_existing_file_edit(self) -> None:
        read_files = '{"tool":"read_files","args":{"paths":["app.py"]}}'
        edit = '{"tool":"edit","args":{"path":"app.py","old_string":"old","new_string":"new"}}'
        done = '{"tool":"done","args":{"summary":"updated"}}'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")

            result = run_agent(
                FakeProvider(read_files, edit, done),
                root,
                "update app",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")

        self.assertEqual(result.stop_reason, "done")

    def test_parallel_read_file_unlocks_existing_file_edit(self) -> None:
        read_parallel = (
            '{"tool":"parallel","args":{"calls":['
            '{"tool":"read_file","args":{"path":"app.py"}},'
            '{"tool":"list_dir","args":{"path":"."}}'
            ']}}'
        )
        edit = '{"tool":"edit","args":{"path":"app.py","old_string":"old","new_string":"new"}}'
        done = '{"tool":"done","args":{"summary":"updated"}}'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")

            result = run_agent(
                FakeProvider(read_parallel, edit, done),
                root,
                "update app",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")

        self.assertEqual(result.stop_reason, "done")

    def test_read_files_runs_reads_serially_and_returns_ordered_results(self) -> None:
        read_files = (
            '{"tool":"read_files","args":{"paths":["a.py","b.py","c.py","d.py"]}}'
        )
        done = '{"tool":"done","args":{"summary":"read all"}}'
        seen: list[str] = []
        paths = ("a.py", "b.py", "c.py", "d.py")

        def read_probe(_root: Path, rel: str, **_options: object):
            seen.append(rel)
            return tool_runtime.ToolOutcome(f"{rel}: ok\n", True)

        events = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in paths:
                (root / name).write_text(f"{name}\n", encoding="utf-8")

            result = run_agent(
                FakeProvider(read_files, done),
                root,
                "read files",
                on_event=events.append,
                fresh_chat=False,
                tool_fns=AgentToolFns(read_file=read_probe),
            )

        tool_events = [event for event in events if event.kind == "tool"]
        tool_lifecycle = [
            (event.kind, event.call.args["path"])
            for event in events
            if event.kind in {"tool_start", "tool"}
        ]
        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(seen, list(paths))
        self.assertEqual(
            [event.call.args["path"] for event in tool_events],
            list(paths),
        )
        self.assertEqual(
            tool_lifecycle,
            [
                ("tool_start", "a.py"),
                ("tool", "a.py"),
                ("tool_start", "b.py"),
                ("tool", "b.py"),
                ("tool_start", "c.py"),
                ("tool", "c.py"),
                ("tool_start", "d.py"),
                ("tool", "d.py"),
            ],
        )

    def test_same_turn_read_flushes_before_existing_file_edit(self) -> None:
        reply = (
            '{"tool":"read_file","args":{"path":"app.py"}}\n'
            '{"tool":"edit","args":{"path":"app.py",'
            '"old_string":"old","new_string":"new"}}'
        )
        done = '{"tool":"done","args":{"summary":"updated"}}'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")

            result = run_agent(
                FakeProvider(reply, done),
                root,
                "read and edit",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")

        self.assertEqual(result.stop_reason, "done")

    def test_references_boundary_keeps_all_tools_serial(self) -> None:
        reply = (
            '{"tool":"grep","args":{"path":".","query":"SessionStore"}}\n'
            '{"tool":"find_references","args":{"path":".","symbol":"SessionStore"}}\n'
            '{"tool":"read_file","args":{"path":"session.py"}}'
        )
        done = '{"tool":"done","args":{"summary":"checked"}}'
        calls: list[str] = []

        def record(name: str, output: str):
            calls.append(name)
            return tool_runtime.ToolOutcome(output, True)

        def search_probe(_root: Path, _rel: str, _query: str):
            return record("search", "session.py:1: SessionStore")

        def references_probe(_root: Path, _rel: str, _symbol: str):
            return record("references", "References for SessionStore under .:")

        def read_probe(_root: Path, _rel: str, **_options: object):
            return record("read", "class SessionStore:\n    pass\n")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "session.py").write_text(
                "class SessionStore:\n    pass\n",
                encoding="utf-8",
            )

            result = run_agent(
                FakeProvider(reply, done),
                root,
                "inspect references",
                on_event=lambda _event: None,
                fresh_chat=False,
                tool_fns=AgentToolFns(
                    read_file=read_probe,
                    search_files=search_probe,
                    find_references=references_probe,
                ),
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(calls, ["search", "references", "read"])

    def test_serial_readonly_tool_exception_becomes_tool_error(self) -> None:
        parallel = (
            '{"tool":"parallel","args":{"calls":['
            '{"tool":"read_file","args":{"path":"app.py"}},'
            '{"tool":"grep","args":{"query":"boom","path":"."}}'
            ']}}'
        )
        done = '{"tool":"done","args":{"summary":"handled"}}'

        def read_probe(_root: Path, _rel: str, **_options: object):
            return tool_runtime.ToolOutcome("ok\n", True)

        def search_probe(_root: Path, _rel: str, _query: str):
            raise RuntimeError("boom")

        events = []
        with tempfile.TemporaryDirectory() as td:
            result = run_agent(
                FakeProvider(parallel, done),
                Path(td),
                "handle search failure",
                on_event=events.append,
                fresh_chat=False,
                tool_fns=AgentToolFns(
                    read_file=read_probe,
                    search_files=search_probe,
                ),
            )

        search_events = [
            event for event in events
            if event.kind == "tool"
            and event.call is not None
            and event.call.name == "search"
        ]
        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(len(search_events), 1)
        self.assertFalse(search_events[0].outcome.ok)
        self.assertIn("ERROR: boom", search_events[0].outcome.model_text)

    def test_run_tool_passes_turn_and_index_to_context_tool_fn(self) -> None:
        reply = '{"tool":"run","args":{"path":".","command":"python -m pytest -q"}}'
        done = '{"tool":"done","args":{"summary":"verified"}}'
        seen = []

        def run_probe(
            _root: Path,
            rel: str,
            command: str,
            tool_id: str,
            permission_profile: str,
            phase: str,
        ):
            seen.append((rel, command, tool_id, permission_profile, phase))
            return tool_runtime.ToolOutcome("exit 0: ok", True, exit_code=0)

        with tempfile.TemporaryDirectory() as td:
            result = run_agent(
                FakeProvider(reply, done),
                Path(td),
                "run tests",
                on_event=lambda _event: None,
                fresh_chat=False,
                tool_fns=AgentToolFns(run_command_with_context=run_probe),
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(seen, [(".", "python -m pytest -q", "1:0", "coding_writer", "writer")])

    def test_read_before_edit_uses_canonical_project_paths(self) -> None:
        read = '{"tool":"read_file","args":{"path":"src/app.py"}}'
        edit = (
            '{"tool":"edit","args":{"path":"src/../src/app.py",'
            '"old_string":"old","new_string":"new"}}'
        )
        done = '{"tool":"done","args":{"summary":"updated"}}'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            path = root / "src" / "app.py"
            path.write_text("old\n", encoding="utf-8")

            result = run_agent(
                FakeProvider(read, edit, done),
                root,
                "update app",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")

        self.assertEqual(result.stop_reason, "done")

    def test_read_tool_call_does_not_capture_change_baseline(self) -> None:
        reply = '{"tool":"read_file","args":{"path":"app.py"}}'
        done = '{"tool":"done","args":{"summary":"read"}}'

        class Tracker:
            def __init__(self) -> None:
                self.paths: list[str] = []

            def capture_before(self, rel: str) -> None:
                self.paths.append(rel)

            def capture_after(self, rel: str) -> None:
                self.paths.append(rel)

        tracker = Tracker()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                FakeProvider(reply, done),
                root,
                "read app",
                on_event=lambda _m: None,
                fresh_chat=False,
                change_tracker=tracker,
            )

            self.assertEqual(result.stop_reason, "done")
            self.assertEqual(tracker.paths, [])

    def test_shell_action_requests_approval_and_stops(self) -> None:
        reply = '{"tool":"shell","args":{"path":".","command":"git status --short"}}'
        requests: list[tuple[str, str]] = []
        events = []
        with tempfile.TemporaryDirectory() as td:
            result = run_agent(
                FakeProvider(reply),
                Path(td),
                "check git",
                on_event=events.append,
                on_shell_request=lambda cwd, command: requests.append((cwd, command)),
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "approval")
        self.assertEqual(requests, [(".", "git status --short")])
        self.assertTrue(any("shell approval requested" in render_run_event(event) for event in events))

    def test_shell_action_without_approval_channel_is_policy_denied(self) -> None:
        shell = '{"tool":"shell","args":{"path":".","command":"git status --short"}}'
        done = '{"tool":"done","args":{"summary":"not run"}}'
        events = []
        with tempfile.TemporaryDirectory() as td:
            result = run_agent(
                FakeProvider(shell, done),
                Path(td),
                "check git",
                on_event=events.append,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(result.summary, "not run")
        tool_events = [event for event in events if event.kind == "tool"]
        self.assertEqual(tool_events[0].outcome.error_code, "policy_denied")
        self.assertIn("approval channel", tool_events[0].outcome.model_text)
        self.assertFalse(any("shell approval requested" in render_run_event(event) for event in events))

    def test_done_after_edit_requires_requested_verification_run(self) -> None:
        read = '{"tool":"read_file","args":{"path":"app.py"}}'
        edit = '{"tool":"edit","args":{"path":"app.py","old_string":"return 1","new_string":"return 2"}}'
        premature_done = '{"tool":"done","args":{"summary":"fixed"}}'
        run_tests = '{"tool":"run","args":{"command":"python -m unittest","path":"."}}'
        done = '{"tool":"done","args":{"summary":"fixed and tested"}}'
        provider = FakeProvider(read, edit, premature_done, run_tests, done)
        events = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
            (root / "test_app.py").write_text(
                "import unittest\n"
                "from app import value\n\n"
                "class AppTests(unittest.TestCase):\n"
                "    def test_value(self):\n"
                "        self.assertEqual(value(), 2)\n",
                encoding="utf-8",
            )

            result = run_agent(
                provider,
                root,
                "Fix app.py, then run python -m unittest.",
                on_event=events.append,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(result.summary, "fixed and tested")
        self.assertTrue(result.changed)
        self.assertTrue(result.checks_passed)
        self.assertTrue(any("verification" in render_run_event(event).lower() for event in events))
        self.assertTrue(any("python -m unittest" in prompt for prompt in provider.sent))

    def test_done_after_edit_respects_explicit_no_run_request(self) -> None:
        read = '{"tool":"read_file","args":{"path":"app.py"}}'
        edit = '{"tool":"edit","args":{"path":"app.py","old_string":"VALUE = 1","new_string":"VALUE = 2"}}'
        done = '{"tool":"done","args":{"summary":"updated without running commands"}}'
        provider = FakeProvider(read, edit, done)
        events = []
        candidate = VerificationCandidate("python -m pytest", ".")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / "test_app.py").write_text("import app\n\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "In app.py change VALUE from 1 to 2. Do not run any commands; report done once edited.",
                on_event=events.append,
                fresh_chat=False,
                verification_candidates=(candidate,),
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(result.summary, "updated without running commands")
        self.assertTrue(result.changed)
        self.assertFalse(result.checks_ran)
        self.assertFalse(result.checks_passed)
        self.assertEqual(len(provider.sent), 3)
        self.assertFalse(
            any("The user asked for verification" in prompt for prompt in provider.sent)
        )
        self.assertFalse(
            any("trusted local check is available" in prompt for prompt in provider.sent)
        )
        self.assertFalse(
            any("Changed files needing verification" in prompt for prompt in provider.sent)
        )
        self.assertFalse(
            any("Suggested verification for current changes" in prompt for prompt in provider.sent)
        )
        self.assertFalse(
            any(
                event.kind == "tool"
                and event.call is not None
                and event.call.name == "run"
                for event in events
            )
        )

    def test_negated_setup_command_does_not_hide_explicit_verification_request(self) -> None:
        self.assertFalse(
            agent_protocol.task_requests_verification(
                "Do not run any commands; report done once edited."
            )
        )
        self.assertTrue(
            agent_protocol.task_forbids_verification(
                "Do not run any commands; report done once edited."
            )
        )
        self.assertTrue(
            agent_protocol.task_requests_verification(
                "Do not run npm install; run python -m py_compile app.py after editing."
            )
        )
        self.assertFalse(
            agent_protocol.task_forbids_verification(
                "Do not run npm install; run python -m py_compile app.py after editing."
            )
        )

    def test_requested_verification_requires_run_after_latest_edit(self) -> None:
        run_before_edit = '{"tool":"run","args":{"command":"python -m py_compile app.py","path":"."}}'
        read = '{"tool":"read_file","args":{"path":"app.py"}}'
        edit = '{"tool":"edit","args":{"path":"app.py","old_string":"VALUE = 1","new_string":"VALUE = 2"}}'
        premature_done = '{"tool":"done","args":{"summary":"updated"}}'
        run_after_edit = '{"tool":"run","args":{"command":"python -m py_compile app.py","path":"."}}'
        done = '{"tool":"done","args":{"summary":"updated and checked"}}'
        provider = FakeProvider(
            run_before_edit,
            read,
            edit,
            premature_done,
            run_after_edit,
            done,
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                provider,
                root,
                "Update app.py, then run python -m py_compile app.py.",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertTrue(result.checks_passed)
        self.assertEqual(
            sum(
                "after the latest edit has been observed" in prompt
                for prompt in provider.sent
            ),
            1,
        )

    def test_requested_verification_failure_reaches_completion_proof_layer(self) -> None:
        read = '{"tool":"read_file","args":{"path":"app.py"}}'
        edit = '{"tool":"edit","args":{"path":"app.py","old_string":"return 1","new_string":"return 2"}}'
        run_tests = '{"tool":"run","args":{"command":"python -m unittest","path":"."}}'
        done = '{"tool":"done","args":{"summary":"updated, but tests still fail"}}'
        provider = FakeProvider(read, edit, run_tests, done)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
            (root / "test_app.py").write_text(
                "import unittest\n"
                "from app import value\n\n"
                "class AppTests(unittest.TestCase):\n"
                "    def test_value(self):\n"
                "        self.assertEqual(value(), 3)\n",
                encoding="utf-8",
            )

            result = run_agent(
                provider,
                root,
                "Fix app.py, then run python -m unittest.",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertTrue(result.changed)
        self.assertFalse(result.checks_passed)
        self.assertEqual(
            sum(
                "after the latest edit has been observed" in prompt
                for prompt in provider.sent
            ),
            0,
        )

    def test_edit_after_successful_run_requires_fresh_check(self) -> None:
        run = '{"tool":"run","args":{"command":"python -m py_compile app.py","path":"."}}'
        read = '{"tool":"read_file","args":{"path":"app.py"}}'
        edit = '{"tool":"edit","args":{"path":"app.py","old_string":"VALUE = 1","new_string":"VALUE = 2"}}'
        done = '{"tool":"done","args":{"summary":"updated"}}'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                FakeProvider(run, read, edit, done),
                root,
                "update app",
                on_event=lambda _m: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertFalse(result.checks_passed)

    def test_failed_run_after_success_clears_checks_passed(self) -> None:
        run_ok = '{"tool":"run","args":{"command":"python -m py_compile app.py","path":"."}}'
        read = '{"tool":"read_file","args":{"path":"app.py"}}'
        edit = '{"tool":"edit","args":{"path":"app.py","old_string":"VALUE = 1","new_string":"VALUE = "}}'
        run_fail = '{"tool":"run","args":{"command":"python -m py_compile app.py","path":"."}}'
        done = '{"tool":"done","args":{"summary":"updated"}}'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = run_agent(
                FakeProvider(run_ok, read, edit, run_fail, done),
                root,
                "update app",
                on_event=lambda _m: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertFalse(result.checks_passed)

    def test_default_verification_reminds_once_and_passes(self) -> None:
        candidate = VerificationCandidate("python -m py_compile app.py")
        replies = (
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"edit","args":{"path":"app.py","old_string":"VALUE = 1","new_string":"VALUE = 2"}}',
            '{"tool":"done","args":{"summary":"updated"}}',
            '{"tool":"run","args":{"command":"python -m py_compile app.py","path":"."}}',
            '{"tool":"done","args":{"summary":"updated and checked"}}',
        )
        provider = FakeProvider(*replies)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            result = run_agent(
                provider,
                root,
                "update app",
                fresh_chat=False,
                on_event=lambda _event: None,
                verification_candidates=(candidate,),
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertTrue(result.checks_passed)
        self.assertEqual(
            sum("trusted local check" in prompt for prompt in provider.sent), 1
        )

    def test_proactive_compatible_check_avoids_default_reminder(self) -> None:
        candidate = VerificationCandidate("python -m py_compile app.py")
        provider = FakeProvider(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"edit","args":{"path":"app.py","old_string":"VALUE = 1","new_string":"VALUE = 2"}}',
            '{"tool":"run","args":{"command":"python -m py_compile app.py","path":"."}}',
            '{"tool":"done","args":{"summary":"checked"}}',
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            result = run_agent(
                provider,
                root,
                "update app",
                fresh_chat=False,
                on_event=lambda _event: None,
                verification_candidates=(candidate,),
            )

        self.assertTrue(result.checks_passed)
        self.assertFalse(any("trusted local check" in prompt for prompt in provider.sent))

    def test_scoped_pytest_does_not_satisfy_default_unittest_candidate(self) -> None:
        candidate = VerificationCandidate("python -m unittest discover")
        provider = FakeProvider(
            '{"tool":"read_file","args":{"path":"src/app.py"}}',
            '{"tool":"edit","args":{"path":"src/app.py","old_string":"return 1","new_string":"return 2"}}',
            '{"tool":"run","args":{"command":"python -m pytest tests/old.py","path":"."}}',
            '{"tool":"done","args":{"summary":"checked"}}',
            '{"tool":"run","args":{"command":"python -m unittest discover","path":"."}}',
            '{"tool":"done","args":{"summary":"checked with suite"}}',
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "src" / "__init__.py").write_text("", encoding="utf-8")
            (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
            (root / "src" / "app.py").write_text(
                "def value():\n    return 1\n",
                encoding="utf-8",
            )
            (root / "tests" / "test_app.py").write_text(
                "from src.app import value\n\n"
                "import unittest\n\n"
                "class AppTests(unittest.TestCase):\n"
                "    def test_value(self):\n"
                "        self.assertEqual(value(), 2)\n",
                encoding="utf-8",
            )
            (root / "tests" / "old.py").write_text(
                "def test_value():\n"
                "    assert True\n",
                encoding="utf-8",
            )

            result = run_agent(
                provider,
                root,
                "update app",
                fresh_chat=False,
                on_event=lambda _event: None,
                verification_candidates=(candidate,),
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertTrue(result.checks_passed)
        self.assertEqual(
            sum("trusted local check" in prompt for prompt in provider.sent),
            1,
        )

    def test_default_verification_does_not_repeat_after_failure(self) -> None:
        candidate = VerificationCandidate("python -m py_compile app.py")
        provider = FakeProvider(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"edit","args":{"path":"app.py","old_string":"VALUE = 1","new_string":"VALUE ="}}',
            '{"tool":"done","args":{"summary":"updated"}}',
            '{"tool":"run","args":{"command":"python -m py_compile app.py","path":"."}}',
            '{"tool":"done","args":{"summary":"could not verify"}}',
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            result = run_agent(
                provider,
                root,
                "update app",
                fresh_chat=False,
                on_event=lambda _event: None,
                verification_candidates=(candidate,),
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertFalse(result.checks_passed)
        self.assertEqual(
            sum("trusted local check" in prompt for prompt in provider.sent), 1
        )

    def test_checkpoint_green_check_is_reused(self) -> None:
        candidate = VerificationCandidate("python -m py_compile app.py")
        provider = FakeProvider('{"tool":"done","args":{"summary":"resumed"}}')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            result = run_agent(
                provider,
                root,
                "continue update",
                fresh_chat=False,
                on_event=lambda _event: None,
                verification_candidates=(candidate,),
                verification_changed_files=("app.py",),
                verification_successful_checks=(candidate,),
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertTrue(result.checks_passed)
        self.assertEqual(len(provider.sent), 1)

    def test_new_edit_after_failed_default_check_gets_new_reminder(self) -> None:
        candidate = VerificationCandidate("python -m py_compile app.py")
        provider = FakeProvider(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"edit","args":{"path":"app.py","old_string":"VALUE = 1","new_string":"VALUE ="}}',
            '{"tool":"done","args":{"summary":"updated"}}',
            '{"tool":"run","args":{"command":"python -m py_compile app.py","path":"."}}',
            '{"tool":"edit","args":{"path":"app.py","old_string":"VALUE =","new_string":"VALUE = 2"}}',
            '{"tool":"done","args":{"summary":"fixed"}}',
            '{"tool":"run","args":{"command":"python -m py_compile app.py","path":"."}}',
            '{"tool":"done","args":{"summary":"fixed and checked"}}',
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            result = run_agent(
                provider,
                root,
                "update app",
                fresh_chat=False,
                on_event=lambda _event: None,
                verification_candidates=(candidate,),
            )

        self.assertTrue(result.checks_passed)
        self.assertEqual(
            sum("trusted local check" in prompt for prompt in provider.sent), 2
        )

    def test_unrelated_green_run_does_not_satisfy_default_candidate(self) -> None:
        candidate = VerificationCandidate("python -m pytest")
        provider = FakeProvider(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"edit","args":{"path":"app.py","old_string":"VALUE = 1","new_string":"VALUE = 2"}}',
            '{"tool":"run","args":{"command":"python -m py_compile other.py","path":"."}}',
            '{"tool":"done","args":{"summary":"updated"}}',
            '{"tool":"done","args":{"summary":"not verified"}}',
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / "other.py").write_text("VALUE = 0\n", encoding="utf-8")
            result = run_agent(
                provider,
                root,
                "update app",
                fresh_chat=False,
                on_event=lambda _event: None,
                verification_candidates=(candidate,),
            )

        self.assertFalse(result.checks_passed)
        self.assertEqual(
            sum("trusted local check" in prompt for prompt in provider.sent), 1
        )

    def test_default_candidate_refresh_discovers_new_manifest(self) -> None:
        provider = FakeProvider(
            '{"tool":"edit","args":{"path":"pytest.ini","content":"[pytest]\\n"}}',
            '{"tool":"done","args":{"summary":"created config"}}',
            '{"tool":"done","args":{"summary":"not verified"}}',
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = run_agent(
                provider,
                root,
                "create project config",
                fresh_chat=False,
                on_event=lambda _event: None,
                verification_candidate_loader=lambda: (
                    VerificationCandidate("python -m pytest"),
                ),
            )

        self.assertFalse(result.checks_passed)
        self.assertEqual(
            sum("trusted local check" in prompt for prompt in provider.sent), 1
        )

    def test_default_candidate_refresh_drops_removed_manifest_command(self) -> None:
        stale = VerificationCandidate("npm test")
        provider = FakeProvider(
            '{"tool":"read_file","args":{"path":"app.js"}}',
            '{"tool":"edit","args":{"path":"app.js","old_string":"old","new_string":"new"}}',
            '{"tool":"done","args":{"summary":"updated"}}',
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.js").write_text("old\n", encoding="utf-8")
            result = run_agent(
                provider,
                root,
                "update app metadata",
                fresh_chat=False,
                on_event=lambda _event: None,
                verification_candidates=(stale,),
                verification_candidate_loader=lambda: (),
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertFalse(result.checks_passed)
        self.assertFalse(any("trusted local check" in prompt for prompt in provider.sent))


class ProtocolTelemetryTests(unittest.TestCase):
    def test_native_tool_denial_counts_error_repair_prompt_and_valid_turn(self) -> None:
        denial = (
            "Sorry, the read_file tool does not exist on this website, "
            "so I cannot proceed."
        )
        done = '{"tool":"done","args":{"summary":"finished"}}'
        provider = FakeProvider(denial, done)
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(Path(td) / "state")
            trace = store.open(
                run_id="run-telemetry",
                session_id="session-telemetry",
                project=Path(td),
                mode_initial="project",
                provider_initial="fake",
            )
            result = run_agent(
                provider,
                Path(td),
                "Read the page then finish",
                fresh_chat=False,
                on_event=lambda _event: None,
                trace_recorder=trace,
            )
            trace.finish(status=result.stop_reason)

            payload = json.loads(
                store.path_for("session-telemetry", "run-telemetry").read_text(
                    encoding="utf-8",
                )
            )

        self.assertEqual(result.stop_reason, "done")
        writer = payload["protocol_telemetry"]["phases"]["writer"]
        self.assertEqual(writer["codec_name"], "json")
        self.assertEqual(writer["protocol_error_counts"], {"native_tool_denial": 1})
        self.assertEqual(writer["repair_prompt_counts"], {"native_tool_denial": 1})
        self.assertEqual(writer["repair_prompt_count"], 1)
        # Turn 1 denied the protocol; turn 2 parsed the repaired reply.
        self.assertEqual(writer["valid_turns"], [2])
        self.assertEqual(writer["first_valid_turn"], 2)

    def test_terminal_protocol_failure_sends_and_counts_one_fewer_repair_prompts(self) -> None:
        # Four invalid replies exhaust DEFAULT_STAGNANT_TURNS: three repair
        # prompts go out between them, the terminal failure sends none.
        bad = "I cannot produce JSON right now."
        provider = FakeProvider(bad, bad, bad, bad)
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(Path(td) / "state")
            trace = store.open(
                run_id="run-telemetry-terminal",
                session_id="session-telemetry-terminal",
                project=Path(td),
                mode_initial="project",
                provider_initial="fake",
            )
            result = run_agent(
                provider,
                Path(td),
                "Read the page then finish",
                fresh_chat=False,
                on_event=lambda _event: None,
                trace_recorder=trace,
            )
            trace.finish(status=result.stop_reason)

            payload = json.loads(
                store.path_for("session-telemetry-terminal", "run-telemetry-terminal").read_text(
                    encoding="utf-8",
                )
            )

        self.assertEqual(result.stop_reason, "protocol")
        writer = payload["protocol_telemetry"]["phases"]["writer"]
        self.assertEqual(sum(writer["protocol_error_counts"].values()), 4)
        self.assertEqual(sum(writer["repair_prompt_counts"].values()), 3)
        # Intro plus three repairs; nothing was sent after the terminal error.
        self.assertEqual(len(provider.sent), 4)

    def test_empty_json_stagnation_counts_every_no_json_observation(self) -> None:
        # A reply that parses as JSON but carries no calls/control takes the
        # stagnation path: every such send is a no_json observation too,
        # including the terminal one -- sends and observations stay 1:1
        # while repair prompts still count only real nudges.
        empty = '{"args":{}}'
        provider = FakeProvider(empty, empty, empty, empty)
        with tempfile.TemporaryDirectory() as td:
            store = RunTraceStore(Path(td) / "state")
            trace = store.open(
                run_id="run-telemetry-empty-json",
                session_id="session-telemetry-empty-json",
                project=Path(td),
                mode_initial="project",
                provider_initial="fake",
            )
            result = run_agent(
                provider,
                Path(td),
                "Read the page then finish",
                fresh_chat=False,
                on_event=lambda _event: None,
                trace_recorder=trace,
            )
            trace.finish(status=result.stop_reason)

            payload = json.loads(
                store.path_for("session-telemetry-empty-json", "run-telemetry-empty-json").read_text(
                    encoding="utf-8",
                )
            )

        self.assertEqual(result.stop_reason, "no_progress")
        writer = payload["protocol_telemetry"]["phases"]["writer"]
        self.assertEqual(writer["protocol_error_counts"], {"no_json": 4})
        self.assertEqual(sum(writer["repair_prompt_counts"].values()), 3)
        self.assertEqual(len(provider.sent), 4)

    def test_unknown_tool_lands_as_safe_label_with_digest(self) -> None:
        unknown = '{"tool":"write_file","args":{"path":"x.py","content":"hi"}}'
        edit = (
            '{"tool":"edit","args":{"path":"app.py",'
            '"old_string":"a","new_string":"b"}}'
        )
        done = '{"tool":"done","args":{"summary":"created"}}'
        provider = FakeProvider(unknown, edit, done)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("a\n", encoding="utf-8")
            store = RunTraceStore(root / "state")
            trace = store.open(
                run_id="run-unknown-tool",
                session_id="session-unknown-tool",
                project=root,
                mode_initial="project",
                provider_initial="fake",
            )
            result = run_agent(
                provider,
                root,
                "Create the file with a local tool",
                fresh_chat=False,
                on_event=lambda _event: None,
                trace_recorder=trace,
            )
            trace.finish(status=result.stop_reason)

            serialized = (
                store.path_for("session-unknown-tool", "run-unknown-tool")
                .read_text(encoding="utf-8")
            )

        tools = json.loads(serialized)["protocol_telemetry"]["phases"]["writer"][
            "unknown_tools"
        ]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["label"], "write_file")
        self.assertTrue(tools[0]["digest"].startswith("sha256:"))

    def test_turn_with_tool_call_and_no_control_continues_and_returns_results(self) -> None:
        from codey.protocols.json_codec import JsonToolCodec, ToolPlan

        class NoControlCodec(JsonToolCodec):
            def parse(self, text: str) -> ToolPlan:
                plan = super().parse(text)
                if plan.calls:
                    return ToolPlan(calls=plan.calls, control=None)
                return plan

        # Turn 1: tool call only, no control element
        reply1 = json.dumps({"tool": "read_file", "args": {"path": "main.py"}})
        # Turn 2: model sees results and finishes with done
        reply2 = json.dumps({"tool": "done", "args": {"summary": "finished reading"}})
        provider = FakeProvider(reply1, reply2)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "main.py").write_text("print('hello')\n", encoding="utf-8")
            events: list[str] = []
            result = run_agent(
                provider,
                root,
                "read main.py",
                codec=NoControlCodec(),
                fresh_chat=False,
                on_event=lambda ev: events.append(render_run_event(ev)),
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(result.summary, "finished reading")
        self.assertEqual(len(provider.sent), 2)
        self.assertIn("print('hello')", provider.sent[1])
        self.assertIn("Please remember to include a <continue> or <done>", provider.sent[1])

    def test_agent_run_records_tool_argument_repair_telemetry(self) -> None:
        read = '{"tool":"grep","args":{"pattern":"test_needle","path":"src/sub"}}'
        done = '{"tool":"done","args":{"summary":"finished searching"}}'
        provider = FakeProvider(read, done)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src" / "sub").mkdir(parents=True)
            (root / "src" / "sub" / "app.py").write_text("test_needle = 123\n", encoding="utf-8")

            store = RunTraceStore(root / "state")
            trace = store.open(
                run_id="run-telemetry",
                session_id="session-telemetry",
                project=root,
                mode_initial="project",
                provider_initial="fake",
            )
            result = run_agent(
                provider,
                root,
                "find needle",
                fresh_chat=False,
                trace_recorder=trace,
            )
            trace.finish(status=result.stop_reason)

            payload = json.loads(
                store.path_for("session-telemetry", "run-telemetry").read_text(
                    encoding="utf-8",
                )
            )

        self.assertEqual(result.stop_reason, "done")
        writer_telemetry = payload["protocol_telemetry"]["phases"]["writer"]
        self.assertGreater(writer_telemetry.get("alias_rewrite_count", 0), 0)
        self.assertIn("search_field_alias", writer_telemetry.get("arg_repair_counts", {}))


if __name__ == "__main__":
    unittest.main()
