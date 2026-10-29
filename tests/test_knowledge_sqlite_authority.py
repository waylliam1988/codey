from dataclasses import replace

import pytest

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore


def test_migration_restart_export_and_rebuild_have_one_authority(tmp_path):
    note = KnowledgeNote(id="legacy", title="Legacy", body="Original", aliases=["别名"])
    path = tmp_path / "notes" / "legacy.md"
    path.parent.mkdir()
    path.write_text(note.to_markdown(), encoding="utf-8")
    store = KnowledgeStore(tmp_path)
    try:
        assert store.read_note(note.id).aliases == ["别名"]
        store.write_note(replace(note, body="Revised"))
        path.write_text(note.to_markdown(), encoding="utf-8")
        store.index.clear()
        assert store.rebuild() == 1
        assert store.read_note(note.id).body == "Revised"
        assert store.read_note(note.id, revision=1).body == "Original"
    finally:
        store.close()
    store = KnowledgeStore(tmp_path)
    try:
        assert store.read_note(note.id).body == "Revised"
        with pytest.raises(ValueError, match="revision conflict"):
            store.import_markdown(path.read_text(encoding="utf-8"), expected_revision=1)
        assert store.export_note(note.id).read_text(encoding="utf-8") == store.read_note(note.id).to_markdown()
    finally:
        store.close()


def test_failed_transaction_leaves_no_content_history_or_index(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="crash") , store.index.transaction():
            store.write_note(KnowledgeNote(id="aborted", title="Abort", body="Invisible"))
            raise RuntimeError("crash")
        assert store.read_note("aborted") is None
        assert store.documents.history("aborted") == []
        assert store.index.search("Invisible") == []
    finally:
        store.close()


def test_compensation_retains_history_and_detects_later_edits(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        note = KnowledgeNote(id="versioned", title="Title", body="Before")
        store.write_note(note)
        changes = KnowledgeChanges(tmp_path)
        store.write_note(replace(note, body="During"), changes=changes)
        assert changes.restore_result().ok
        assert store.read_note(note.id).body == "Before"
        assert [v["reason"] for v in store.documents.history(note.id)] == ["edit", "edit", "compensation"]
        store.write_note(replace(note, body="Again"), changes=changes)
        store.write_note(replace(note, body="Later"))
        assert not changes.restore_result().ok
        assert store.read_note(note.id).body == "Later"
    finally:
        store.close()


def test_export_failure_does_not_undo_committed_knowledge(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path)
    try:
        store.write_note(KnowledgeNote(id="saved", body="Saved"))
        monkeypatch.setattr("codey.knowledge.store.write_text_atomic", lambda *a: (_ for _ in ()).throw(OSError("full")))
        with pytest.raises(OSError):
            store.export_note("saved")
        assert store.read_note("saved").body == "Saved"
    finally:
        store.close()
