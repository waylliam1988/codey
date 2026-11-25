"""Markdown parsing failures remain local to the affected knowledge note."""

from contextlib import closing
from pathlib import Path

import pytest

from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore


@pytest.mark.parametrize("tags", ["[unclosed", "5"])
def test_malformed_frontmatter_does_not_block_legacy_notes_or_enter_the_store(tmp_path: Path, tags: str) -> None:
    malformed = f"---\nid: bad\ntags: {tags}\n---\nBad note.\n"
    note = KnowledgeNote(id="valid", type="fact", title="Valid", body="An intact legacy note.")
    (tmp_path / "bad.md").write_text(malformed, encoding="utf-8")
    (tmp_path / "valid.md").write_text(note.to_markdown(), encoding="utf-8")

    with closing(KnowledgeStore(tmp_path)) as store:
        assert store.read_note("valid") == note
        assert store.read_note("bad") is None
        with pytest.raises(ValueError, match="frontmatter"):
            store.import_markdown(malformed, expected_revision=0)
        assert store.read_note("bad") is None
