from __future__ import annotations

import unittest
from pathlib import Path

from codey.runtime.events import RunEvent, display_tool, run_event_ui_payload
from codey.runtime.models import ToolCall
from codey.toolchain.runtime import ToolOutcome


ROOT = Path(__file__).resolve().parents[1]


class RunEventUiPayloadTests(unittest.TestCase):
    def test_research_tools_use_existing_ui_display_mapping(self) -> None:
        cases = (
            ("web_search", {"query": "helium"}, "search", "helium"),
            ("open_url", {"url": "https://example.com/a"}, "read", "https://example.com/a"),
            ("knowledge_search", {"query": "facts"}, "recall", "facts"),
            ("knowledge_read", {"note_id": "note-1"}, "note", "note-1"),
            ("knowledge_write", {"title": "Title"}, "note", "Title"),
            ("knowledge_link", {"src": "note-a"}, "link", "note-a"),
        )

        for name, args, kind, label in cases:
            with self.subTest(name=name):
                self.assertEqual(display_tool(name, args), (kind, label))

    def test_run_event_ui_payload_keeps_tool_shape(self) -> None:
        call = ToolCall("run", {"path": ".", "command": "python -m pytest"})
        outcome = ToolOutcome(
            "exit 0",
            True,
            presentation={"status": "ok", "result": "exit 0"},
            audit={
                "managed_output": {
                    "handle": "out_0001_valid",
                    "original_bytes": 10,
                    "stored_bytes": 8,
                    "sha256": "a" * 64,
                }
            },
            exit_code=0,
            truncated=True,
        )
        event = RunEvent.tool_finished(3, call, outcome, index=2)

        payload = run_event_ui_payload("run-1", "session-1", event)

        self.assertEqual(payload, {
            "type": "tool",
            "run_id": "run-1",
            "session_id": "session-1",
            "turn": 3,
            "tool_id": "3:2",
            "kind": "run",
            "path": "",
            "result": "exit 0",
            "status": "ok",
            "error": False,
            "ok": True,
            "changed": False,
            "truncated": True,
            "command": "python -m pytest",
            "exit_code": 0,
            "output_handle": "out_0001_valid",
            "output_bytes": 10,
            "output_stored_bytes": 8,
            "output_sha256": "a" * 64,
        })

    def test_task_runner_no_longer_owns_ui_event_projection(self) -> None:
        source = (ROOT / "codey" / "app" / "task_runner.py").read_text(encoding="utf-8")

        self.assertNotIn("def _ui_event", source)
        self.assertNotIn("def _display_tool", source)
        self.assertIn("run_event_ui_payload", source)


if __name__ == "__main__":
    unittest.main()
