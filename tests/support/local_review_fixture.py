"""Scripted local model and non-Git project for formal review entry tests."""
from __future__ import annotations

import json

from codey.providers.local_openai import LocalOpenAIProvider


class ScriptedLocal(LocalOpenAIProvider):
    def __init__(self, name, replies, timeline):
        super().__init__("http://localhost:5001/v1", "scripted-model")
        self.label = name
        self.replies = iter(replies)
        self.timeline = timeline
        self.prompts = []
        self.chats = 0
        self.closed = 0

    def new_chat(self):
        self.chats += 1
        self.timeline.append((self.label, "new_chat"))

    def send(self, text, timeout=None):
        self.prompts.append(text)
        self.timeline.append((self.label, "send"))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply)

    def close(self):
        self.closed += 1


def writer_turns(before, after):
    return [
        {"tool": "read_file", "args": {"path": "math_utils.py"}},
        {"tool": "edit", "args": {"path": "math_utils.py", "replacements": [{"old_string": before, "new_string": after}]}},
        {"tool": "run", "args": {"command": "python -m unittest discover", "path": "."}},
        {"tool": "done", "args": {"summary": "Implemented addition and verified."}},
    ]


def fixture_project(tmp_path):
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
