"""Document revisions track the location as well as Markdown content."""

import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path

import pytest

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore


def test_path_change_with_same_markdown_is_a_new_document_revision(tmp_path: Path) -> None:
    with closing(KnowledgeStore(tmp_path)) as store:
        documents = store.documents
        note = KnowledgeNote("manual", title="Manual", body="Existing text")
        old_path, new_path = "notes/manual.md", "notes/current/manual.md"
        premise = KnowledgeObject("premise", "claim", "Linked context", "model_inference")
        assert store.publish_note(note, "manual-publication", [KnowledgeMutation(premise)]) == old_path
        assert documents.write(note, old_path) == 1
        assert documents.write(note, new_path, reason="compensation") == 2
        assert documents.get(note.id, 1) == (note, old_path, 1)
        assert documents.by_path(old_path) is None
        assert documents.by_path(new_path) == (note, new_path, 2)
        assert store.read_notes_with_rows([note.id])[note.id][1]["path"] == new_path
        references: Sequence[Mapping[str, object]] = store.note_references(note.id)
        assert references == [{"target": "premise", "revision": 1}]
        assert documents.write(note, new_path) == 2


def test_store_write_moves_identical_imported_note_and_records_undo(tmp_path: Path) -> None:
    note = KnowledgeNote("legacy", title="Legacy", body="Existing text")
    old_path = "legacy.md"

    with closing(KnowledgeStore(tmp_path)) as store:
        store.documents.write(note, old_path, reason="import")
        changes = KnowledgeChanges(tmp_path)
        new_path = store.write_note(note, changes=changes)
        assert new_path == "notes/legacy.md"
        assert store.documents.by_path(new_path) == (note, new_path, 2)
        assert store.documents.by_path(old_path) is None
        assert store.write_note(note) == new_path
        assert store.documents.by_path(new_path) == (note, new_path, 2)

        restored = changes.restore_result()
        assert restored.ok
        assert store.documents.by_path(new_path) is None
        assert store.documents.by_path(old_path) == (note, old_path, 4)


def test_failed_note_write_leaves_undo_snapshot_intact(tmp_path: Path) -> None:
    with closing(KnowledgeStore(tmp_path)) as store:
        note = KnowledgeNote("reject", updated="2025-01-01T00:00:00+00:00")
        changes = KnowledgeChanges(tmp_path)
        before = changes.snapshot()
        with store.index.transaction() as conn:
            conn.execute(
                "CREATE TRIGGER reject_note BEFORE INSERT ON note_documents "
                "WHEN NEW.id = 'reject' BEGIN SELECT RAISE(ABORT, 'rejected note'); END"
            )

        with pytest.raises(sqlite3.IntegrityError, match="rejected note"):
            store.write_note(note, changes=changes)

        assert changes.snapshot() == before
        assert note.updated == "2025-01-01T00:00:00+00:00"
        assert store.documents.history(note.id) == []
