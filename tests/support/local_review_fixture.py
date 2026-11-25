"""Scripted local model and non-Git project for formal review entry tests."""
from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from codey.providers.api_provider import ApiProvider
from codey.types.json_value import JsonObject
from codey.types.protocol_window import ProtocolWindowRef


class ScriptedLocal(ApiProvider):
    def __init__(self, name: str, replies: Iterable[JsonObject | Exception], timeline: list[tuple[str, str]]) -> None:
        super().__init__("http://localhost:5001/v1", "scripted-model")
        self.label = name
        self.replies = iter(replies)
        self.timeline = timeline
        self.prompts: list[str] = []
        self.chats = 0
        self.closed = 0

    def _record_chat(self) -> None:
        self.chats += 1
        self.timeline.append((self.label, "new_chat"))

    def new_chat(self, timeout: float | None = None) -> None:
        self._record_chat()

    def reset_window(self, expected_ref: ProtocolWindowRef | None = None) -> ProtocolWindowRef:
        window = super().reset_window(expected_ref=expected_ref)
        self._record_chat()
        return window

    def activate_durable_window(self, messages: list[dict[str, object]], *, source_entry_ids: tuple[str, ...],
                                reason: str) -> ProtocolWindowRef:
        window = super().activate_durable_window(messages, source_entry_ids=source_entry_ids, reason=reason)
        self._record_chat()
        return window

    def send(self, text: str, timeout: float | None = None, **request_options: object) -> str:
        # ApiProvider receives request-scoped tool/context arguments. This
        # fixture scripts decisions; transport serialization is tested elsewhere.
        self.prompts.append(text)
        self.timeline.append((self.label, "send"))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply)

    def close(self) -> None:
        self.closed += 1


def writer_turns(before: str, after: str) -> list[JsonObject]:
    return [
        {"tool": "read_file", "args": {"path": "math_utils.py"}},
        {"tool": "edit", "args": {"path": "math_utils.py", "replacements": [{"old_string": before, "new_string": after}]}},
        {"tool": "run", "args": {"command": "python -m unittest discover", "path": "."}},
        {"tool": "done", "args": {"summary": "Implemented addition and verified."}},
    ]


def fixture_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "math_utils.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    tests = project / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_math.py").write_text(
        "import unittest\nfrom math_utils import add\nclass Addition(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n", encoding="utf-8")
    return project
