from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
