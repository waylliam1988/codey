"""Real project entry: write/verify, automatic isolated local review, one repair."""
from __future__ import annotations

import json
from functools import partial

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.providers.local_openai import LocalOpenAIProvider
from codey.reviews.persistence import load_recorded_review


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


@pytest.mark.parametrize("finding", [False, True])
@pytest.mark.parametrize("desktop_routing", [False, True])
def test_project_automatically_reviews_with_same_local_model_and_repairs_once(tmp_path, monkeypatch, finding, desktop_routing):
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    project = fixture_project(tmp_path)
    timeline = []
    before = "def add(a, b):\n    return a - b\n"
    written = "def add(a, b):\n    return a + b\n"
    repaired = 'def add(a, b):\n    """Return the sum."""\n    return a + b\n'
    writer = ScriptedLocal("writer", writer_turns(before, written), timeline)
    repair = ScriptedLocal("repair", writer_turns(written, repaired), timeline)
    reply = {"verdict": "approved", "summary": "Correct addition.", "findings": []}
    if finding:
        reply = {"verdict": "changes_requested", "summary": "Add requested documentation.",
                 "findings": [{"path": "math_utils.py", "issue": "The requested docstring is missing.",
                               "suggested_fix": "Add a docstring to add."}]}
    reviewer = ScriptedLocal("reviewer", [reply], timeline)
    def connect_reviewer(provider_id):
        assert provider_id == "local"
        return reviewer

    if desktop_routing:
        # Use the same run_review callback as the desktop, with no open web
        # reviewer. Only its transport connector is scripted.
        from codey.app.review_service import run_review

        monkeypatch.setattr("codey.app.review_service.providers.reviewer_candidates", lambda *_: ())
        monkeypatch.setattr("codey.app.review_service.providers.connect_fresh_provider_tab", connect_reviewer)
        monkeypatch.setattr("codey.app.headless_runner._headless_review_for", lambda _request, state, _connector: partial(run_review, state))
    writers = iter((writer, repair))
    rows = []
    result = run_headless(
        HeadlessRequest(project=project, task="Fix add to return the sum and document it.",
                        provider_id="local", intent="project", max_turns=8, state_home=tmp_path / "state"),
        emit_jsonl=rows.append, connect_provider=lambda *_a, **_k: next(writers),
        connect_reviewer=connect_reviewer,
    )
    assert result.stop_reason == "done", rows
    assert result.exit_code == 0
    assert reviewer.chats == 1, rows
    assert writer.chats == 1
    assert len(reviewer.prompts) == 1
    assert reviewer.closed == 1
    assert "return a + b" in reviewer.prompts[0]
    assert timeline.index(("reviewer", "new_chat")) > max(index for index, item in enumerate(timeline) if item == ("writer", "send"))
    assert (project / "math_utils.py").read_text() == (repaired if finding else written)
    assert len(repair.prompts) == (4 if finding else 0)
    terminal = [row for row in rows if row["type"] == "task_done"]
    assert len(terminal) == 1
    assert terminal[0]["receipt"]["verification"]["checks_passed"] is True
    review_event = next(row for row in rows if row["type"] == "review" and isinstance(row.get("review"), dict))
    assert rows.index(review_event) < rows.index(terminal[0])
    recorded = load_recorded_review(tmp_path / "state", result.session_id, result.run_id)
    assert recorded is not None
    assert recorded[1].identity.self_review is True
    assert recorded[1].identity.model_id
    tools = [row for row in rows if row["type"] == "tool" and row.get("ok") is True]
    assert sum(row["tool_name"] == "edit" for row in tools) == (2 if finding else 1)
    assert sum(row["tool_name"] == "run" for row in tools) == (2 if finding else 1)


def test_project_unknown_review_result_is_not_retried_or_repaired(tmp_path, monkeypatch):
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    project = fixture_project(tmp_path)
    timeline, rows = [], []
    before = "def add(a, b):\n    return a - b\n"
    written = "def add(a, b):\n    return a + b\n"
    writer = ScriptedLocal("writer", writer_turns(before, written), timeline)
    reviewer = ScriptedLocal("reviewer", [ConnectionError("reply lost after send")], timeline)
    result = run_headless(
        HeadlessRequest(project=project, task="Fix addition.", provider_id="local", intent="project", max_turns=8, state_home=tmp_path / "state"),
        emit_jsonl=rows.append, connect_provider=lambda *_a, **_k: writer,
        connect_reviewer=lambda *_: reviewer,
    )
    assert result.stop_reason == "done"  # Advisory review does not replace verification.
    assert len(writer.prompts) == 4
    assert len(reviewer.prompts) == 1
    assert reviewer.closed == 1
    assert load_recorded_review(tmp_path / "state", result.session_id, result.run_id) is None
    assert not any(row.get("review", {}).get("status") == "complete" for row in rows)
    assert any(row.get("type") == "review" and "unavailable" in row.get("text", "").lower() for row in rows)


def test_default_headless_project_does_not_add_reviewer_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    monkeypatch.setattr("codey.app.review_service.run_review", lambda *_a, **_k: pytest.fail("default project must not invoke Reviewer"))
    project = fixture_project(tmp_path)
    writer = ScriptedLocal("writer", writer_turns("return a - b", "return a + b"), [])
    result = run_headless(
        HeadlessRequest(project=project, task="Fix addition.", provider_id="local", intent="project", max_turns=8, state_home=tmp_path / "state"),
        emit_jsonl=lambda _: None, connect_provider=lambda *_a, **_k: writer,
    )
    assert result.stop_reason == "done"
    assert len(writer.prompts) == 4


def test_reviewer_connector_for_non_project_intent_is_rejected_before_creating_state(tmp_path):
    project = tmp_path / "not-created"
    with pytest.raises(ValueError, match="requires project or review intent"):
        run_headless(HeadlessRequest(project=project, task="Hello", intent="chat", state_home=tmp_path / "state"),
                     emit_jsonl=lambda _: pytest.fail("must not emit"),
                     connect_reviewer=lambda *_: pytest.fail("must not connect"))
    assert not project.exists()
    assert not (tmp_path / "state").exists()
