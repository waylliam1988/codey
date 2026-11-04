"""Committed display is task state, never a fabricated read or verification."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from codey.research.answer_context import prior_knowledge
from codey.runtime.core.models import ToolCall, ToolResult
from tests.support.research_committed_state_candidate import committed_prior
from tests.test_research_partial_edit_candidate import setup


def add_commit(session, value, *, ok=True, revision=2):
    call = ToolCall("knowledge_commit", {"commit_id": "edit", "changes": [
        {"expected_revision": 1, "value": {"id": value.id, "title": "New title"}}]})
    session._memory_results["edit"] = ToolResult(call, json.dumps({
        "commit_id": "edit", "versions": {value.id: revision}}), ok=ok)


def test_saved_projection_retains_fields_and_before_edit_without_mutating_receipts(tmp_path):
    store, value, session = setup(tmp_path)
    try:
        add_commit(session, value)
        original = deepcopy(session._memory_results)
        rows = committed_prior(session, prior_knowledge)
        assert len(rows) == 1
        row = rows[0]
        assert row["revision"] == 2 and row["read_revision_before_edit"] == 1
        assert row["commit_id"] == "edit" and "recorded_at" not in row
        assert row["title"] == "New title" and row["before_edit"] == {"title": ""}
        for key in ("content", "conditions", "references", "scope_id", "origin", "aliases"):
            assert row[key] == json.loads(original["read"].model_text)[key]
        assert "not independently verified evidence" in row["snapshot_state"]
        assert session._memory_results == original
        assert store.repository.read(value.id).revision == 1  # Projection is not a database write.
    finally:
        store.close()


@pytest.mark.parametrize("failed", [True, False])
def test_no_successful_edit_keeps_original_read_only_view_exactly(tmp_path, failed):
    store, value, session = setup(tmp_path)
    try:
        if failed:
            add_commit(session, value, ok=False)
        assert committed_prior(session, prior_knowledge) == prior_knowledge(session)
    finally:
        store.close()


def test_small_budget_omits_whole_saved_snapshot_with_exact_recovery(tmp_path):
    store, value, session = setup(tmp_path)
    try:
        add_commit(session, value)
        row = committed_prior(session, prior_knowledge, budget=400)[0]
        assert row["display_omitted"] is True and "content" not in row and "conditions" not in row
        assert row["recover"] == {"tool": "knowledge_read", "args": {"id": value.id, "revision": 2}}
        assert row["revision"] == 2
    finally:
        store.close()


def test_unread_commit_cannot_replace_read_snapshot_and_duplicate_reads_collapse(tmp_path):
    store, value, session = setup(tmp_path)
    try:
        add_commit(session, replace(value, id="different"))
        assert committed_prior(session, prior_knowledge) == prior_knowledge(session)
        add_commit(session, value)
        later = json.loads(session._memory_results["read"].model_text)
        later.update(revision=2, title="New title")
        session._memory_results["read-after"] = replace(session._memory_results["read"], model_text=json.dumps(later))
        rows = committed_prior(session, prior_knowledge)
        assert len(rows) == 1 and rows[0]["revision"] == 2
    finally:
        store.close()
