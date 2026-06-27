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


class DefaultsTests(unittest.TestCase):
    def test_default_turn_limit_is_shared_with_server(self) -> None:
        from codey import server

        self.assertEqual(agent.DEFAULT_MAX_TURNS, 50)
        self.assertIs(server.DEFAULT_MAX_TURNS, agent.DEFAULT_MAX_TURNS)


if __name__ == "__main__":
    unittest.main()
