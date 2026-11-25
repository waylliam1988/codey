from __future__ import annotations

import sqlite3
from contextlib import closing, nullcontext
from dataclasses import replace
from pathlib import Path

import pytest

from codey.knowledge.index import KnowledgeIndex
from codey.knowledge.note import KnowledgeNote


@pytest.mark.parametrize("nested", [False, True])
def test_failed_index_update_rolls_back_before_the_next_write(tmp_path: Path, nested: bool) -> None:
    with closing(KnowledgeIndex(tmp_path / "knowledge.db")) as index:
        note = KnowledgeNote(id="original", title="Original", body="Original", tags=["original"])
        index.upsert(note, path="notes/original.md", content_hash="original")
        with index.transaction() as conn:
            conn.execute(
                "CREATE TRIGGER reject_source BEFORE INSERT ON sources "
                "WHEN NEW.source = 'invalid' BEGIN SELECT RAISE(ABORT, 'invalid source'); END"
            )

        with index.transaction() if nested else nullcontext():
            with pytest.raises(sqlite3.IntegrityError, match="invalid source"):
                index.upsert(
                    replace(note, body="Partial", tags=["partial"], sources=["invalid"]),
                    path="notes/original.md",
                    content_hash="partial",
                )
            index.upsert(KnowledgeNote(id="later"), path="notes/later.md", content_hash="later")

        original = index.get(note.id)
        assert original is not None and original["body"] == "Original"
        assert index.tags_for([note.id]) == [{"note_id": note.id, "tag": "original"}]
        assert index.get("later") is not None
