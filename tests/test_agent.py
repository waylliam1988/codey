from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from codey import agent


class FakeProvider:
    name = "Fake Provider"
    location = "fake://provider"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.new_chat_calls = 0

    def new_chat(self) -> None:
        self.new_chat_calls += 1

    def send(self, _text: str, timeout: float | None = None) -> str:
        del timeout
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
        text = '{"tool":"grep","args":{"path":"src","pattern":"login handler"}}'
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
{"tool":"grep","args":{"path":".","pattern":"LEGACY_BUG"}}
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
                agent._safe_join(root, "../escape.txt")

    def test_write_read_and_ls(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            result = agent.tool_write(root, "src/app.py", "print('ok')\n")
            self.assertIn("wrote src/app.py", result)

            self.assertEqual(agent.tool_read(root, "src/app.py"), "print('ok')\n")
            listing = agent.tool_ls(root, ".")
            self.assertIn("src/", listing)
            self.assertIn("app.py", listing)

    def test_read_missing_file_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                agent.tool_read(Path(td), "missing.py"),
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

            output = agent.tool_search(root, ".", "LOGIN_HANDLER")

            self.assertIn("src/app.py:1: def login_handler():", output)

    def test_search_skips_excluded_directories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "node_modules").mkdir()
            (root / "node_modules" / "lib.js").write_text("login_handler\n", encoding="utf-8")

            self.assertEqual(agent.tool_search(root, ".", "login_handler"), "(no matches)")

    def test_search_requires_query(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                agent.tool_search(Path(td), ".", "   "),
                "ERROR: search query required",
            )

    def test_edit_replaces_unique_search_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("def old():\n    return 1\n", encoding="utf-8")
            body = """<<<<<<< SEARCH
def old():
=======
def new():
>>>>>>> REPLACE"""

            result = agent.tool_edit(root, "app.py", body)

            self.assertEqual(result, "edited app.py (1 replacement)")
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "def new():\n    return 1\n")

    def test_edit_rejects_non_unique_search_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("same\nsame\n", encoding="utf-8")
            body = """<<<<<<< SEARCH
same
=======
other
>>>>>>> REPLACE"""

            result = agent.tool_edit(root, "app.py", body)

            self.assertEqual(result, "ERROR: SEARCH text matched 2 times in app.py; make it unique")
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "same\nsame\n")

    def test_edit_rejects_missing_search_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("current\n", encoding="utf-8")
            body = """<<<<<<< SEARCH
missing
=======
replacement
>>>>>>> REPLACE"""

            self.assertEqual(
                agent.tool_edit(root, "app.py", body),
                "ERROR: SEARCH text not found in app.py",
            )

    def test_run_allows_py_compile(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "ok.py").write_text("VALUE = 1\n", encoding="utf-8")

            output = agent.tool_run(root, ".", "python -m py_compile ok.py")

            self.assertIn("exit 0: python -m py_compile ok.py", output)

    def test_run_rejects_dangerous_shell_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = agent.tool_run(Path(td), ".", "python -m unittest && rm -rf .")

            self.assertEqual(output, "ERROR: command not allowed: python -m unittest && rm -rf .")

    def test_run_rejects_non_allowlisted_command(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output = agent.tool_run(Path(td), ".", "git status")

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

            output = agent.tool_run(root, ".", "python -m unittest")

            self.assertIn("exit 0: python -m unittest", output)
            self.assertFalse((root / "__pycache__").exists())


class ProjectInstructionTests(unittest.TestCase):
    def test_missing_project_instructions_returns_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(agent.load_project_instructions(Path(td)), [])

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
            self.assertEqual(docs[0].content, "abc\n\n[truncated by Codey]")


class DefaultsTests(unittest.TestCase):
    def test_default_turn_limit_is_shared_with_server(self) -> None:
        from codey import server

        self.assertEqual(agent.DEFAULT_MAX_TURNS, 50)
        self.assertIs(server.DEFAULT_MAX_TURNS, agent.DEFAULT_MAX_TURNS)


class RunLoopTests(unittest.TestCase):
    def test_edit_tool_call_updates_file(self) -> None:
        reply = '{"tool":"edit","args":{"path":"app.py","old_string":"return \'old\'","new_string":"return \'new\'"}}'
        done = '{"tool":"done","args":{"summary":"updated"}}'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("def value():\n    return 'old'\n", encoding="utf-8")

            result = agent.run(
                FakeProvider(reply, done),
                root,
                "update app",
                on_event=lambda _m: None,
                fresh_chat=False,
            )

            self.assertEqual(result.stop_reason, "done")
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "def value():\n    return 'new'\n")

    def test_edit_tool_call_captures_change_baseline(self) -> None:
        reply = '{"tool":"edit","args":{"path":"app.py","old_string":"old","new_string":"new"}}'
        done = '{"tool":"done","args":{"summary":"updated"}}'

        class Tracker:
            def __init__(self) -> None:
                self.paths: list[str] = []

            def capture_before(self, rel: str) -> None:
                self.paths.append(rel)

        tracker = Tracker()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("old\n", encoding="utf-8")

            result = agent.run(
                FakeProvider(reply, done),
                root,
                "update app",
                on_event=lambda _m: None,
                fresh_chat=False,
                change_tracker=tracker,
            )

            self.assertEqual(result.stop_reason, "done")
            self.assertEqual(tracker.paths, ["app.py"])

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

    def test_read_tool_call_does_not_capture_change_baseline(self) -> None:
        reply = '{"tool":"read_file","args":{"path":"app.py"}}'
        done = '{"tool":"done","args":{"summary":"read"}}'

        class Tracker:
            def __init__(self) -> None:
                self.paths: list[str] = []

            def capture_before(self, rel: str) -> None:
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
        events: list[str] = []
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
        self.assertTrue(any("shell approval requested" in event for event in events))


if __name__ == "__main__":
    unittest.main()
