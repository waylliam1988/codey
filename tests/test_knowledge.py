from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.knowledge import (
    KnowledgeBriefBuilder,
    KnowledgeChanges,
    KnowledgeGraphBuilder,
    KnowledgeNote,
    KnowledgeStore,
)
from codey.knowledge.note import LINK_KINDS


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

    def test_brief_builder_extracts_report_quality_context(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            note = KnowledgeNote.create(
                type="synthesis",
                title="Helium research",
                body=(
                    "## 结论\n"
                    "- Build the tracker [1]\n\n"
                    "## 关键证据\n"
                    "- [1] Source supports the feature.\n\n"
                    "## 反证与限制\n"
                    "- 未找到强反证；supplier data could overturn this.\n\n"
                    "## 来源质量\n"
                    "- [1] secondary · web · undated · example.com\n\n"
                    "## 搜索覆盖\n"
                    "- query: helium\n\n"
                    "## 来源\n"
                    "[1] Helium source - https://example.com/helium\n"
                ),
                sources=["https://example.com/helium"],
                session_id="s1",
            )
            store.write_note(note)

            rendered = KnowledgeBriefBuilder(store).build_for_session("s1").render()
            store.close()

        self.assertIn("Citation map", rendered)
        self.assertIn("[1] Helium source", rendered)
        self.assertIn("Counter-evidence / limitations", rendered)
        self.assertIn("supplier data could overturn this", rendered)
        self.assertIn("Source quality risks", rendered)
        self.assertIn("secondary", rendered)

    def test_index_graph_queries_return_neighbors_and_sources(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            a = KnowledgeNote.create(
                type="synthesis",
                title="Synthesis",
                body="A",
                sources=["https://example.com/a"],
                session_id="s1",
            )
            b = KnowledgeNote.create(type="fact", title="Fact", body="B", sources=["https://example.com/b"])
            c = KnowledgeNote.create(type="question", title="Question", body="C")
            store.write_note(a)
            store.write_note(b)
            store.write_note(c)
            store.link(a.id, b.id, "supports")
            store.link(c.id, a.id, "contradicts")

            rows = store.index.notes_by_ids([b.id, a.id])
            links = store.index.links_touching([a.id])
            sources = store.index.sources_for([a.id])
            store.close()

        self.assertEqual([row["id"] for row in rows], [b.id, a.id])
        self.assertIn({"src_id": a.id, "dst_id": b.id, "kind": "supports"}, links)
        self.assertIn({"src_id": c.id, "dst_id": a.id, "kind": "contradicts"}, links)
        self.assertEqual(sources, [{"note_id": a.id, "source": "https://example.com/a"}])

    def test_graph_builder_adds_source_url_and_virtual_counterpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            synthesis = KnowledgeNote.create(
                type="synthesis",
                title="Research graph",
                body="Use the evidence.",
                sources=["https://example.com/research"],
                session_id="s1",
            )
            fact = KnowledgeNote.create(
                type="fact",
                title="Grounded fact",
                body="The source supports the claim.",
                sources=["https://example.com/fact"],
                session_id="s1",
            )
            store.write_note(synthesis)
            store.write_note(fact)
            store.link(synthesis.id, fact.id, "derives")

            graph = KnowledgeGraphBuilder(store).build_for_session(
                "s1",
                focus_ids=(synthesis.id, fact.id),
                depth=1,
                counterpoints=("No primary data was found.",),
            ).to_dict()
            store.close()

        node_ids = {node["id"] for node in graph["nodes"]}
        edge_kinds = {edge["kind"] for edge in graph["edges"]}
        focus_ids = {node["id"] for node in graph["nodes"] if node["focus"]}
        self.assertIn(synthesis.id, node_ids)
        self.assertIn(fact.id, node_ids)
        self.assertEqual(graph["center_id"], synthesis.id)
        self.assertEqual(focus_ids, {synthesis.id, fact.id})
        self.assertTrue(any(node["kind"] == "source_url" for node in graph["nodes"]))
        self.assertTrue(any(node["kind"] == "counterpoint" for node in graph["nodes"]))
        self.assertIn("cites", edge_kinds)
        self.assertIn("contradicts", edge_kinds)
        self.assertNotIn("cites", LINK_KINDS)

    def test_graph_builder_depth_and_real_contradicts_are_respected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            synthesis = KnowledgeNote.create(type="synthesis", title="Run", body="S", session_id="s1")
            implementation = KnowledgeNote.create(type="implementation", title="Impl", body="I", session_id="s1")
            verification = KnowledgeNote.create(type="verification", title="Verify", body="V", session_id="s1")
            question = KnowledgeNote.create(type="question", title="Counter", body="Q", session_id="s1")
            for note in (synthesis, implementation, verification, question):
                store.write_note(note)
            store.link(synthesis.id, implementation.id, "implements")
            store.link(implementation.id, verification.id, "verifies")
            store.link(synthesis.id, question.id, "contradicts")

            depth_one = KnowledgeGraphBuilder(store).build_for_session(
                "s1",
                focus_ids=(synthesis.id,),
                depth=1,
                counterpoints=("Virtual counterpoint should be suppressed.",),
            ).to_dict()
            depth_two = KnowledgeGraphBuilder(store).build_for_session(
                "s1",
                focus_ids=(synthesis.id,),
                depth=2,
            ).to_dict()
            store.close()

        depth_one_ids = {node["id"] for node in depth_one["nodes"]}
        depth_two_ids = {node["id"] for node in depth_two["nodes"]}
        self.assertIn(implementation.id, depth_one_ids)
        self.assertNotIn(verification.id, depth_one_ids)
        self.assertIn(verification.id, depth_two_ids)
        self.assertFalse(any(node["kind"] == "counterpoint" for node in depth_one["nodes"]))
        self.assertIn("verifies", {edge["kind"] for edge in depth_two["edges"]})

    def test_graph_focus_ids_only_include_rendered_nodes_after_limit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            synthesis = KnowledgeNote.create(type="synthesis", title="Run", body="S", session_id="s1")
            facts = [
                KnowledgeNote.create(type="fact", title=f"Fact {idx}", body=f"F{idx}", session_id="s1")
                for idx in range(12)
            ]
            store.write_note(synthesis)
            for fact in facts:
                store.write_note(fact)
                store.link(synthesis.id, fact.id, "derives")

            graph = KnowledgeGraphBuilder(store).build_for_session(
                "s1",
                focus_ids=(synthesis.id, *(fact.id for fact in facts)),
                depth=1,
                node_limit=8,
                include_sources=False,
            ).to_dict()
            store.close()

        node_ids = {node["id"] for node in graph["nodes"]}
        self.assertEqual(graph["center_id"], synthesis.id)
        self.assertLessEqual(len(graph["nodes"]), 8)
        self.assertTrue(set(graph["focus_ids"]).issubset(node_ids))
        self.assertEqual(
            {node["id"] for node in graph["nodes"] if node["focus"]},
            set(graph["focus_ids"]),
        )


if __name__ == "__main__":
    unittest.main()
