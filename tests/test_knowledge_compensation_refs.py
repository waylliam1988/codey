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



def test_answer_documents_assemble_the_exact_answer_revision(tmp_path: Path) -> None:
    with closing(KnowledgeStore(tmp_path)) as store:
        note = KnowledgeNote("report", title="Answer", body="A unique settled answer.")
        answer = KnowledgeObject("answer", "answer", note.body, "model_inference",
            provenance={"basis": "settled_whole_answer_submission", "synthesis_id": note.id})
        path = store.publish_note(note, "publish", [KnowledgeMutation(answer)])
        assert store.read_note(note.id) == note
        assert store.documents.by_path(path) == (note, path, 1)
        assert store.search_notes("unique settled")[0]["id"] == note.id
        assert store.documents.get(note.id, 1) == (note, path, 1)
        assert store.publish_note(note, "publish", [KnowledgeMutation(answer)]) == path
        assert len(store.documents.history(note.id)) == 1

        second = replace(answer, title="A new version with the same body")
        store.publish_note(note, "republish", [KnowledgeMutation(second, 1)])
        assert store.note_references(note.id) == [{"target": answer.id, "revision": 2}]
        assert store.note_references(note.id, 1) == [{"target": answer.id, "revision": 1}]
        assert store.read_note(note.id, revision=1) == note
        assert store.read_note(note.id, revision=2) == note
        with store.index.transaction() as conn:
            rows: list[tuple[int, str]] = conn.execute(
                "SELECT revision,markdown FROM note_revisions WHERE id=? ORDER BY revision", (note.id,),
            ).fetchall()
        assert [(revision, KnowledgeNote.from_markdown(markdown).body) for revision, markdown in rows] == [
            (1, ""), (2, ""),
        ]
