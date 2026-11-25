"""Compensating a document restores its version-pinned knowledge references."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore


def test_compensation_restores_published_answer_references(tmp_path: Path) -> None:
    with closing(KnowledgeStore(tmp_path)) as store:
        note = KnowledgeNote("report", title="Original title", body="A settled answer.")
        provenance = {"basis": "settled_whole_answer_submission", "synthesis_id": note.id}
        answer = KnowledgeObject(
            "answer", "answer", note.body, "model_inference",
            provenance=provenance,
        )
        path = store.publish_note(note, "publish", [KnowledgeMutation(answer)])
        changes = KnowledgeChanges(tmp_path)
        store.write_note(replace(note, title="Edited title"), changes=changes)
        store.commit_knowledge("update-answer-metadata", [KnowledgeMutation(replace(answer, title="Retitled"), 1)])

        restored = changes.restore_result()

        assert restored.ok
        assert restored.restored == [path]
        assert store.read_note(note.id) == note
        assert store.note_references(note.id) == [{"target": answer.id, "revision": 1}]
        assert [row["reason"] for row in store.documents.history(note.id)] == ["edit", "edit", "compensation"]
        with pytest.raises(ValueError, match="read-only projection"):
            store.write_note(replace(note, body="An unversioned edit."))
