from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.knowledge import KnowledgeBriefBuilder, KnowledgeChanges, KnowledgeNote, KnowledgeStore


class KnowledgeStoreTests(unittest.TestCase):
    def test_write_read_rebuild_and_search_markdown_notes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            note = KnowledgeNote.create(
                type="fact",
                title="Helium supply constraint",
                body="Helium supply depends on gas field separation.",
                tags=["research", "session:s1"],
                sources=["https://example.com/helium"],
                session_id="s1",
            )

            rel = store.write_note(note)
            loaded = store.read_note(note.id)
            rebuilt = store.rebuild()
            rows = store.index.search("helium")
            store.close()

        self.assertEqual(rel, f"facts/{note.id}.md")
        self.assertRegex(note.id, r"^\d{8}T\d{6}-helium-supply-constraint-[a-f0-9]{8}$")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.title, "Helium supply constraint")
        self.assertEqual(rebuilt, 1)
        self.assertEqual(rows[0]["id"], note.id)

    def test_generated_note_ids_do_not_collide_for_repeated_titles(self) -> None:
        notes = [
            KnowledgeNote.create(type="fact", title="Repeated Title", body="A", sources=["https://example.com/a"])
            for _ in range(20)
        ]

        self.assertEqual(len({note.id for note in notes}), len(notes))

    def test_chinese_search_falls_back_to_substring_when_fts_misses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            note = KnowledgeNote.create(
                type="fact",
                title="氦气供应限制",
                body="氦气供应限制来自天然气分离和储备波动。",
                sources=["https://example.com/helium"],
            )
            store.write_note(note)

            rows = store.index.search("供应")
            store.close()

        self.assertTrue(any(row["id"] == note.id for row in rows))

    def test_changes_restore_removes_new_notes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            changes = KnowledgeChanges(store.root)
            note = KnowledgeNote.create(
                type="synthesis",
                title="Run",
                body="Report",
                session_id="s1",
            )
            store.write_note(note, changes=changes)

            result = changes.restore_result()
            store.rebuild()
            missing = store.read_note(note.id)
            count = store.index.count()
            store.close()

        self.assertTrue(result.ok)
        self.assertEqual(result.restored, [f"synthesis/{note.id}.md"])
        self.assertIsNone(missing)
        self.assertEqual(count, 0)

    def test_brief_builder_uses_latest_session_synthesis_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            other = KnowledgeNote.create(
                type="synthesis",
                title="Other",
                body="Do not include",
                session_id="other",
            )
            current = KnowledgeNote.create(
                type="synthesis",
                title="Helium tool",
                body=(
                    "结论:\n"
                    "- Build the tracker.\n\n"
                    "风险:\n"
                    "- Data may stale.\n"
                ),
                sources=["https://example.com/helium"],
                session_id="s1",
            )
            store.write_note(other)
            store.write_note(current)

            rendered = KnowledgeBriefBuilder(store).build_for_session("s1").render()
            store.close()

        self.assertIn("Research context from this chat", rendered)
        self.assertIn(current.id, rendered)
        self.assertIn("Build the tracker", rendered)
        self.assertIn("https://example.com/helium", rendered)
        self.assertNotIn(other.id, rendered)


if __name__ == "__main__":
    unittest.main()
