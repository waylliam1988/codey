"""Preparation refreshes facts once; rendering consumes an immutable projection."""

from dataclasses import FrozenInstanceError

import pytest

from codey.completion.verification_policy import VerificationCandidate
from codey.operations.project_prompt_context import prepare_coding_context
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.workspace.coding_context import CodingContext, render_coding_context


def test_preparation_refreshes_candidates_once_for_latest_edit():
    loaded = []
    candidate = VerificationCandidate("python -m pytest", ".")
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.read", "project.verify"})),
                          edited_files={"app.py": 2}, read_files={"app.py"},
                          verification_candidate_loader=lambda: loaded.append(True) or (candidate,))
    first = prepare_coding_context(session)
    second = prepare_coding_context(session)
    assert first is not None and second is not None
    assert loaded == [True]
    assert first.selected_verification is candidate
    assert session.verification_candidates_epoch == 2
    assert first.verification_fresh is False
    assert "Changed files needing verification: app.py" in render_coding_context(first)


def test_prepared_context_is_immutable_and_detached_from_session():
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.read", "project.write"})),
                          read_files={"app.py"})
    context = prepare_coding_context(session)
    assert context is not None
    session.read_files.add("later.py")
    assert context.read_files == ("app.py",)
    assert context.edit_eligible_files == ("app.py",)
    with pytest.raises(FrozenInstanceError):
        context.verification_fresh = True


def test_native_suggested_verification_is_not_a_json_reply_instruction():
    text = render_coding_context(CodingContext(
        changed_files=("app.py",), selected_verification=VerificationCandidate("python -m pytest", "tests"),
    ), native=True)
    assert "python -m pytest (path: tests)" in text
    assert '"tool":"run"' not in text
    assert "Reply with exactly one JSON object" not in text
    assert "Call exactly one native tool" in text


def test_context_preparation_passes_the_actual_completion_context(monkeypatch):
    from codey.operations import project_prompt_context

    contexts = []
    monkeypatch.setattr(project_prompt_context, "project_completion_checks",
                        lambda session, context: contexts.append(context) or [])
    supplied = {"workspace_ignored_paths": ("generated",), "project": "project"}
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"project.read"})))
    prepare_coding_context(session, completion_context=supplied)
    assert contexts == [supplied]
