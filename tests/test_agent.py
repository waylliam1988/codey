from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

from codey import agent
from codey.events import render_run_event
from codey.handoff import ConversationContext, ConversationSnapshot
from codey import tool_runtime


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

    def close(self) -> None:
        pass


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
            self.assertIn("wrote src/app.py", result.output)

            self.assertEqual(tool_runtime.read_file(root, "src/app.py").output, "print('ok')\n")
            listing = tool_runtime.list_directory(root, ".").output
            self.assertIn("src/", listing)
            self.assertIn("app.py", listing)

    def test_read_missing_file_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                tool_runtime.read_file(Path(td), "missing.py").output,
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

            output = tool_runtime.search_files(root, ".", "LOGIN_HANDLER").output

            self.assertIn("src/app.py:1: def login_handler():", output)

    def test_search_explains_that_regex_syntax_is_literal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("def login_mask():\n", encoding="utf-8")

            outcome = tool_runtime.search_files(root, ".", "def .*mask")

        self.assertTrue(outcome.ok)
        self.assertEqual(
            outcome.output,
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
        self.assertIn("large.py:60001: def target_after_old_limit():", outcome.output)

    def test_search_reports_oversized_files_instead_of_clean_no_matches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "huge.py").write_text("x" * 33, encoding="utf-8")
            with mock.patch("codey.tool_runtime.SEARCH_MAX_FILE_BYTES", 32):
                outcome = tool_runtime.search_files(root, "huge.py", "target")

        self.assertTrue(outcome.truncated)
        self.assertIn("no literal matches", outcome.output)
        self.assertIn("skipped 1 file(s) larger than 32 bytes", outcome.output)
        self.assertIn("omitted files may contain more matches", outcome.output)

    def test_search_counts_non_utf8_files_toward_the_total_read_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_bytes(b"\xff" * 8)
            (root / "b.py").write_text("target\n", encoding="utf-8")
            with mock.patch("codey.tool_runtime.SEARCH_MAX_SCAN_BYTES", 8):
                outcome = tool_runtime.search_files(root, ".", "target")

        self.assertTrue(outcome.truncated)
        self.assertIn("no literal matches", outcome.output)
        self.assertIn("search scan stopped at 8 bytes read budget", outcome.output)
        self.assertNotIn("b.py", outcome.output)

    def test_search_skips_excluded_directories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "node_modules").mkdir()
            (root / "node_modules" / "lib.js").write_text("login_handler\n", encoding="utf-8")

            self.assertEqual(
                tool_runtime.search_files(root, ".", "login_handler").output,
                "(no literal matches; regex is not supported)",
            )

    def test_search_skips_excluded_directories_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Build").mkdir()
            (root / "Build" / "out.py").write_text("login_handler\n", encoding="utf-8")

            self.assertEqual(
                tool_runtime.search_files(root, ".", "login_handler").output,
                "(no literal matches; regex is not supported)",
            )

    def test_search_skips_direct_excluded_start_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "node_modules").mkdir()
            (root / "node_modules" / "lib.js").write_text("login_handler\n", encoding="utf-8")

            outcome = tool_runtime.search_files(root, "node_modules", "login_handler")

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.output, "(no literal matches; regex is not supported)")

    def test_search_skips_direct_excluded_start_directory_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Node_Modules").mkdir()
            (root / "Node_Modules" / "lib.js").write_text("login_handler\n", encoding="utf-8")

            outcome = tool_runtime.search_files(root, "Node_Modules", "login_handler")

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.output, "(no literal matches; regex is not supported)")

    def test_search_does_not_skip_project_root_named_like_excluded_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "build"
            root.mkdir()
            (root / "app.py").write_text("login_handler\n", encoding="utf-8")

            outcome = tool_runtime.search_files(root, ".", "login_handler")

        self.assertTrue(outcome.ok)
        self.assertIn("app.py:1: login_handler", outcome.output)

    def test_search_reports_scan_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_text("pass\n", encoding="utf-8")
            (root / "b.py").write_text("pass\n", encoding="utf-8")
            (root / "c.py").write_text("login_handler\n", encoding="utf-8")

            with mock.patch("codey.tool_runtime.SEARCH_MAX_SCAN_FILES", 2):
                outcome = tool_runtime.search_files(root, ".", "login_handler")

        self.assertTrue(outcome.truncated)
        self.assertIn("(no literal matches; regex is not supported)", outcome.output)
        self.assertIn("search scan stopped after 2 files", outcome.output)
        self.assertIn("file budget 2", outcome.output)
        self.assertIn("omitted files may contain more matches", outcome.output)
        self.assertNotIn("c.py:1", outcome.output)

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
        self.assertIn("symlink paths are not supported for grep", outcome.output)
        self.assertNotIn("target.py:1", outcome.output)

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
        self.assertIn("symlink paths are not supported for grep", outcome.output)
        self.assertNotIn("target.py:1", outcome.output)

    def test_search_requires_query(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                tool_runtime.search_files(Path(td), ".", "   ").output,
                "ERROR: search query required",
            )

    def test_edit_replaces_unique_search_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("def old():\n    return 1\n", encoding="utf-8")
            blocks = [tool_runtime.EditBlock("def old():", "def new():")]

            result = tool_runtime.edit_file(root, "app.py", blocks).output

            self.assertEqual(result, "edited app.py (1 replacement)")
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "def new():\n    return 1\n")

    def test_edit_rejects_non_unique_search_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("same\nsame\n", encoding="utf-8")
            blocks = [tool_runtime.EditBlock("same", "other")]

            result = tool_runtime.edit_file(root, "app.py", blocks).output

            self.assertIn("SEARCH text matched 2 times in app.py", result)
            self.assertIn("Exact matches start at lines: 1, 2.", result)
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "same\nsame\n")

    def test_edit_rejects_missing_search_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("current\n", encoding="utf-8")
            blocks = [tool_runtime.EditBlock("missing", "replacement")]

            result = tool_runtime.edit_file(root, "app.py", blocks).output

            self.assertIn("SEARCH text not found in app.py", result)
            self.assertIn("Use read_file and copy exact complete lines.", result)

    def test_edit_recovers_unique_leading_indentation_mismatch(self) -> None:
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

            result = tool_runtime.edit_file(root, "app.py", blocks).output

            self.assertEqual(
                result,
                "edited app.py (1 replacement; indentation recovered)",
            )
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "def total():\n"
                "    # target\n"
                "    return (amount - discount) * rate\n",
            )

    def test_edit_rejects_ambiguous_indentation_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(
                "def one():\n"
                "    return amount * rate\n\n"
                "def two():\n"
                "    return amount * rate\n",
                encoding="utf-8",
            )
            blocks = [
                tool_runtime.EditBlock(
                    " return amount * rate",
                    " return (amount - discount) * rate",
                )
            ]

            result = tool_runtime.edit_file(root, "app.py", blocks).output

            self.assertIn("SEARCH text matched 2 times in app.py", result)
            self.assertIn("Exact matches start at lines: 2, 5.", result)
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "def one():\n"
                "    return amount * rate\n\n"
                "def two():\n"
                "    return amount * rate\n",
            )

    def test_run_allows_py_compile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ok.py").write_text("VALUE = 1\n", encoding="utf-8")

            output = tool_runtime.run_command(root, ".", "python -m py_compile ok.py").output

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

            output = tool_runtime.run_command(root, ".", "python -B -m unittest").output

            self.assertIn("exit 0: python -B -m unittest", output)

    def test_run_rejects_dangerous_shell_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = tool_runtime.run_command(Path(td), ".", "python -m unittest && rm -rf .").output

            self.assertEqual(output, "ERROR: command not allowed: python -m unittest && rm -rf .")

    def test_run_rejects_non_allowlisted_command(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = tool_runtime.run_command(Path(td), ".", "git status").output

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

            output = tool_runtime.run_command(root, ".", "python -m unittest").output

            self.assertIn("exit 0: python -m unittest", output)
            self.assertFalse((root / "__pycache__").exists())


class ProjectInstructionTests(unittest.TestCase):
    def test_missing_project_instructions_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(agent.load_project_instructions(Path(td)), [])
            self.assertEqual(agent.format_project_instructions([]), "")

    def test_loads_root_agent_and_claude_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "AGENTS.md").write_text("Use tests first.\n", encoding="utf-8")
            (root / "CLAUDE.md").write_text("Keep changes small.\n", encoding="utf-8")

            docs = agent.load_project_instructions(root)

            self.assertEqual([doc.name for doc in docs], ["AGENTS.md", "CLAUDE.md"])
            self.assertEqual(docs[0].content, "Use tests first.\n")
            self.assertFalse(docs[0].truncated)
            formatted = agent.format_project_instructions(docs)
            self.assertIn("--- AGENTS.md ---", formatted)
            self.assertIn("Keep changes small.", formatted)

    def test_truncates_large_project_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "AGENTS.md").write_text("abcdef", encoding="utf-8")

            docs = agent.load_project_instructions(root, max_chars=3)

            self.assertEqual(len(docs), 1)
            self.assertTrue(docs[0].truncated)
            self.assertEqual(docs[0].content, "abc\n\n[content truncated]")


class DefaultsTests(unittest.TestCase):
    def test_default_turn_limit_is_shared_with_server(self) -> None:
        from codey import server

        self.assertEqual(agent.DEFAULT_MAX_TURNS, 50)
        self.assertIs(server.DEFAULT_MAX_TURNS, agent.DEFAULT_MAX_TURNS)


class RunLoopTests(unittest.TestCase):
    def test_intro_omits_missing_instructions_and_absolute_project_path(self) -> None:
        provider = FakeProvider('{"tool":"done","args":{"summary":"ok"}}')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            agent.run(
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

            agent.run(
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

    def test_read_only_project_discussion_returns_direct_answer_without_changes(self) -> None:
        answer = "Start with one guided breathing rhythm.\n\nAdd customization later."
        provider = FakeProvider(json.dumps({
            "tool": "done",
            "args": {"summary": answer},
        }))

        with tempfile.TemporaryDirectory() as td:
            result = agent.run(
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
            result = agent.run(
                provider,
                Path(td),
                "Review project structure",
                on_event=lambda _event: None,
                project_map="Project Map (bounded local scan; relative paths only):\nManifests:\n- package.json",
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertIn("Project Map", provider.sent[0])
        self.assertIn("package.json", provider.sent[0])

    def test_project_intro_includes_local_execution_checkpoint_when_provided(self) -> None:
        provider = FakeProvider('{"tool":"done","args":{"summary":"resumed"}}')
        with tempfile.TemporaryDirectory() as td:
            result = agent.run(
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

    def test_emits_structured_turn_and_tool_events(self) -> None:
        provider = FakeProvider(
            '{"tool":"read_file","args":{"path":"app.py"}}',
            '{"tool":"done","args":{"summary":"finished"}}',
        )
        events = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = agent.run(
                provider,
                root,
                "Read app.py",
                on_event=events.append,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertFalse(result.changed)
        run_events = [event for event in events if event.kind in {"turn", "tool"}]
        self.assertEqual([event.kind for event in run_events], ["turn", "tool", "turn"])
        self.assertEqual(run_events[1].call.name, "read")
        self.assertTrue(run_events[1].outcome.ok)
        self.assertEqual(run_events[1].outcome.output, "VALUE = 1\n")

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

            result = agent.run(
                provider,
                root,
                "Find calculate_total references",
                on_event=events.append,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertFalse(result.changed)
        tool_events = [event for event in events if event.kind == "tool"]
        self.assertEqual(tool_events[0].call.name, "references")
        self.assertIn("definition pricing.py:1", tool_events[0].outcome.output)
        self.assertIn("[tool_result tool=find_references path=.]", provider.sent[1])
        self.assertIn("lexical scan, not semantic resolution", provider.sent[1])

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

            result = agent.run(
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

            result = agent.run(
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
        self.assertIn("Use edit with content", provider.sent[1])

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

            result = agent.run(
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
        self.assertIn("Call the tool directly", provider.sent[1])

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

            result = agent.run(
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

            result = agent.run(
                provider,
                root,
                "Add the final test",
                on_event=lambda _m: None,
                fresh_chat=False,
                conversation=context,
                provider_id="deepseek",
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertEqual(provider.new_chat_calls, 1)
        self.assertIn("Return only one compact JSON object", provider.sent[0])
        self.assertIn("Factual handoff", provider.sent[1])
        self.assertIn("Finish the original feature", provider.sent[1])
        self.assertIn("Add the final test", provider.sent[1])

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

            result = agent.run(
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

            result = agent.run(
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
                agent.run(
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

            result = agent.run(
                FakeProvider(read, reply, done),
                root,
                "update app",
                on_event=lambda _m: None,
                fresh_chat=False,
            )

            self.assertEqual(result.stop_reason, "done")
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "def value():\n    return 'new'\n")

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

            result = agent.run(
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

            result = agent.run(
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

            result = agent.run(
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

            result = agent.run(
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

            result = agent.run(
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

            result = agent.run(
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

            result = agent.run(
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

            result = agent.run(
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

            result = agent.run(
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

            result = agent.run(
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

            result = agent.run(
                FakeProvider(read_parallel, edit, done),
                root,
                "update app",
                on_event=lambda _event: None,
                fresh_chat=False,
            )

            self.assertEqual(path.read_text(encoding="utf-8"), "new\n")

        self.assertEqual(result.stop_reason, "done")

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

            result = agent.run(
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

            result = agent.run(
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
            result = agent.run(
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

            result = agent.run(
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

    def test_edit_after_successful_run_requires_fresh_check(self) -> None:
        run = '{"tool":"run","args":{"command":"python -m py_compile app.py","path":"."}}'
        read = '{"tool":"read_file","args":{"path":"app.py"}}'
        edit = '{"tool":"edit","args":{"path":"app.py","old_string":"VALUE = 1","new_string":"VALUE = 2"}}'
        done = '{"tool":"done","args":{"summary":"updated"}}'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            result = agent.run(
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

            result = agent.run(
                FakeProvider(run_ok, read, edit, run_fail, done),
                root,
                "update app",
                on_event=lambda _m: None,
                fresh_chat=False,
            )

        self.assertEqual(result.stop_reason, "done")
        self.assertFalse(result.checks_passed)


if __name__ == "__main__":
    unittest.main()
