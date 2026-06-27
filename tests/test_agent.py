from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey import agent


class ParseReplyTests(unittest.TestCase):
    def test_parse_multiple_actions_and_control(self) -> None:
        text = """Some context.

```python
# === codey: read path=app.py ===
```

```text
# === codey: ls path=. ===
```

```text
# === codey: done ===
finished
```
"""
        actions, control = agent.parse_reply(text)

        self.assertEqual([a.kind for a in actions], ["read", "ls"])
        self.assertEqual([a.path for a in actions], ["app.py", "."])
        self.assertIsNotNone(control)
        self.assertEqual(control.kind, "done")
        self.assertEqual(control.body, "finished")

    def test_ignores_non_marker_code_blocks(self) -> None:
        text = """```python
print("hello")
```

```text
# === codey: continue ===
need files
```"""
        actions, control = agent.parse_reply(text)

        self.assertEqual(actions, [])
        self.assertIsNotNone(control)
        self.assertEqual(control.kind, "continue")

    def test_parse_search_action_uses_body_as_query(self) -> None:
        text = """```text
# === codey: search path=src ===
login handler
```

```text
# === codey: continue ===
need the matching files
```"""
        actions, control = agent.parse_reply(text)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "search")
        self.assertEqual(actions[0].path, "src")
        self.assertEqual(actions[0].body, "login handler")
        self.assertIsNotNone(control)
        self.assertEqual(control.kind, "continue")

    def test_parse_inline_marker_payload_from_deepseek(self) -> None:
        text = """```text
=== codey: search path=. ===LEGACY_BUG
```

```text
# === codey: continue ===Need search results
```"""
        actions, control = agent.parse_reply(text)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "search")
        self.assertEqual(actions[0].path, ".")
        self.assertEqual(actions[0].body, "LEGACY_BUG")
        self.assertIsNotNone(control)
        self.assertEqual(control.kind, "continue")
        self.assertEqual(control.body, "Need search results")

    def test_parse_edit_action(self) -> None:
        text = """```text
# === codey: edit path=app.py ===
<<<<<<< SEARCH
old()
=======
new()
>>>>>>> REPLACE
```

```text
# === codey: done ===
updated
```"""
        actions, control = agent.parse_reply(text)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "edit")
        self.assertEqual(actions[0].path, "app.py")
        self.assertIn("<<<<<<< SEARCH", actions[0].body)
        self.assertIsNotNone(control)
        self.assertEqual(control.kind, "done")

    def test_parse_run_action(self) -> None:
        text = """```text
# === codey: run path=. ===
python -m unittest
```

```text
# === codey: continue ===
need test output
```"""
        actions, control = agent.parse_reply(text)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "run")
        self.assertEqual(actions[0].body, "python -m unittest")
        self.assertIsNotNone(control)
        self.assertEqual(control.kind, "continue")

    def test_parse_shell_action(self) -> None:
        text = """```text
# === codey: shell path=. ===
git status --short
```

```text
# === codey: continue ===
waiting for approval
```"""
        actions, control = agent.parse_reply(text)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "shell")
        self.assertEqual(actions[0].body, "git status --short")
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
    def test_shell_action_requests_approval_and_stops(self) -> None:
        reply = """```text
# === codey: shell path=. ===
git status --short
```

```text
# === codey: continue ===
waiting
```"""
        requests: list[tuple[str, str]] = []
        events: list[str] = []
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(agent, "chat", return_value=reply):
                result = agent.run(
                    object(),
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
