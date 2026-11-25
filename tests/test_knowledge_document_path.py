"""Document revisions track the location as well as Markdown content."""

from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path

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
