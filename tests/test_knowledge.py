from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codey.knowledge import (
    KnowledgeBriefBuilder,
    KnowledgeChanges,
    KnowledgeGraphBuilder,
    KnowledgeNote,
    KnowledgeStore,
    UnifiedResearchGraphBuilder,
)
from codey.knowledge.concepts import ConceptGraphBuilder
from codey.knowledge.concept_schema import (
    CONCEPT_EDGE_KINDS,
    clean_relations,
    concept_tags,
    normalize_concept,
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
                body=(
                    "## 结论\n"
                    "Use the evidence.\n\n"
                    "## 来源\n"
                    "[1] Alpha News Title - https://example.com/research\n"
                ),
                sources=["https://example.com/research"],
                session_id="s1",
            )
            fact = KnowledgeNote.create(
                type="fact",
                title="Grounded fact",
                body="## Claim\nThe source supports the claim.\n\n- markdown detail",
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
        research_source = next(
            node for node in graph["nodes"]
            if node["kind"] == "source_url" and node["url"] == "https://example.com/research"
        )
        self.assertEqual(research_source["label"], "Alpha News Title")
        self.assertIn("https://example.com/research", research_source["excerpt"])
        fact_node = next(node for node in graph["nodes"] if node["id"] == fact.id)
        self.assertIn("## Claim", fact_node["excerpt"])
        self.assertIn("- markdown detail", fact_node["excerpt"])
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


class ConceptSchemaTests(unittest.TestCase):
    def test_normalize_concept_canonicalizes_and_rejects_noise(self) -> None:
        self.assertEqual(normalize_concept("  Helium   Supply "), "helium supply")
        self.assertEqual(normalize_concept("(War)"), "war")
        self.assertEqual(normalize_concept("铜"), "铜")
        self.assertEqual(len(normalize_concept("x" * 80)), 48)
        for noise in ("", None, "https://example.com", "www.example.com", "research", "session:s1", "2024", "a"):
            self.assertEqual(normalize_concept(noise), "", msg=repr(noise))

    def test_clean_relations_drops_bad_items_with_warnings(self) -> None:
        raw = [
            {"src": "War", "dst": "Helium", "kind": "AFFECTS"},
            {"src": "war", "dst": "helium", "kind": "affects"},
            {"src": "war", "dst": "war"},
            {"src": "", "dst": "copper"},
            {"src": "copper", "dst": "semiconductor", "kind": "invented_kind"},
            "not-an-object",
        ]

        relations, warnings = clean_relations(raw)

        self.assertEqual(
            relations,
            [
                {"src": "war", "dst": "helium", "kind": "affects"},
                {"src": "copper", "dst": "semiconductor", "kind": "relates"},
            ],
        )
        self.assertEqual(len(warnings), 3)
        self.assertTrue(all(kind in CONCEPT_EDGE_KINDS for kind in ("affects", "relates")))

    def test_clean_relations_enforces_per_note_limit(self) -> None:
        raw = [{"src": "a1", "dst": f"b{idx}"} for idx in range(12)]

        relations, warnings = clean_relations(raw)

        self.assertEqual(len(relations), 8)
        self.assertIn("kept first 8 of 12 relations", warnings)

    def test_concept_tags_filters_machine_tags(self) -> None:
        tags = ["research", "session:s1", "Helium", "helium", "copper", "2023"]

        self.assertEqual(concept_tags(tags), ["helium", "copper"])

    def test_note_relations_roundtrip_frontmatter_and_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            note = KnowledgeNote.create(
                type="fact",
                title="War and helium",
                body="War restricts helium exports.",
                tags=["war", "helium"],
                relations=[{"src": "War", "dst": "Helium", "kind": "affects"}],
                session_id="s1",
            )
            store.write_note(note)

            parsed = KnowledgeNote.from_markdown((store.root / "facts" / f"{note.id}.md").read_text("utf-8"))
            rows_before = store.index.concept_edge_rows()
            store.rebuild()
            rows_after = store.index.concept_edge_rows()
            store.close()

        expected = {
            "note_id": note.id,
            "src": "war",
            "dst": "helium",
            "kind": "affects",
            "session_id": "s1",
            "title": "War and helium",
        }
        self.assertEqual(parsed.relations, [{"src": "war", "dst": "helium", "kind": "affects"}])
        self.assertEqual(rows_before, [expected])
        self.assertEqual(rows_after, [expected])

    def test_note_tolerates_garbage_relations_frontmatter(self) -> None:
        text = (
            "---\n"
            "id: garbage-note\n"
            "type: fact\n"
            "title: Garbage\n"
            "relations:\n"
            "- not a mapping\n"
            "- src: war\n"
            "  dst: war\n"
            "- src: war\n"
            "  dst: copper\n"
            "---\n\nBody\n"
        )

        note = KnowledgeNote.from_markdown(text)

        self.assertEqual(note.relations, [{"src": "war", "dst": "copper", "kind": "relates"}])

    def test_index_remove_and_clear_drop_concept_edges(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            note = KnowledgeNote.create(
                type="fact",
                title="Copper",
                body="B",
                relations=[{"src": "copper", "dst": "semiconductor", "kind": "uses"}],
            )
            store.write_note(note)
            self.assertEqual(len(store.index.concept_edge_rows()), 1)

            store.index.remove(note.id)
            removed = store.index.concept_edge_rows()
            store.write_note(note)
            store.index.clear()
            cleared = store.index.concept_edge_rows()
            store.close()

        self.assertEqual(removed, [])
        self.assertEqual(cleared, [])

    def test_index_tags_for_and_tag_concept_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            note = KnowledgeNote.create(
                type="synthesis",
                title="Run report",
                body="B",
                tags=["research", "helium"],
                session_id="s1",
            )
            old = KnowledgeNote.create(
                type="note",
                title="Old",
                body="B",
                tags=["stale-topic"],
                status="contradicted",
            )
            store.write_note(note)
            store.write_note(old)

            tag_rows = store.index.tags_for([note.id, old.id])
            active_rows = store.index.tags_for([note.id, old.id], active_only=True)
            concept_rows = store.index.tag_concept_rows()
            store.close()

        self.assertEqual({row["tag"] for row in tag_rows}, {"research", "helium", "stale-topic"})
        self.assertEqual({row["tag"] for row in active_rows}, {"research", "helium"})
        self.assertEqual(concept_rows[0]["type"], "synthesis")
        self.assertEqual(concept_rows[0]["session_id"], "s1")

    def test_concept_rows_prioritize_requested_session_before_global_limit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            target = KnowledgeNote.create(
                type="fact",
                title="Target relation",
                body="B",
                tags=["war", "helium"],
                relations=[{"src": "war", "dst": "helium", "kind": "affects"}],
                session_id="target",
            )
            store.write_note(target)
            for i in range(12):
                store.write_note(
                    KnowledgeNote.create(
                        type="fact",
                        title=f"Newer relation {i}",
                        body="B",
                        tags=[f"newer topic {i}"],
                        relations=[
                            {
                                "src": f"newer topic {i}",
                                "dst": f"newer neighbor {i}",
                                "kind": "relates",
                            }
                        ],
                        session_id="newer",
                    )
                )

            edge_rows = store.index.concept_edge_rows(8, session_id="target")
            tag_rows = store.index.tag_concept_rows(8, session_id="target")
            with (
                patch("codey.knowledge.concepts._EDGE_ROW_SCAN", 8),
                patch("codey.knowledge.concepts._TAG_ROW_SCAN", 8),
            ):
                graph = ConceptGraphBuilder(store).build_for_session(
                    "target",
                    node_limit=24,
                    edge_limit=8,
                ).to_dict()
            store.close()

        self.assertEqual(edge_rows[0]["note_id"], target.id)
        self.assertEqual(edge_rows[0]["title"], "Target relation")
        self.assertTrue(any(row["note_id"] == target.id and row["tag"] == "helium" for row in tag_rows))
        node_ids = {node["id"] for node in graph["nodes"]}
        edge_ids = {edge["id"] for edge in graph["edges"]}
        self.assertIn("concept:war", node_ids)
        self.assertIn("concept:helium", node_ids)
        self.assertIn("concept:war->concept:helium:affects", edge_ids)


class ConceptGraphBuilderTests(unittest.TestCase):
    def _store_with_relations(self, td: str) -> KnowledgeStore:
        store = KnowledgeStore(Path(td))
        notes = [
            KnowledgeNote.create(
                type="note", title="N1", body="B", session_id="s1",
                relations=[{"src": "war", "dst": "helium", "kind": "affects"}],
            ),
            KnowledgeNote.create(
                type="note", title="N2", body="B", session_id="s1",
                relations=[{"src": "war", "dst": "helium", "kind": "affects"}],
            ),
            KnowledgeNote.create(
                type="note", title="N3", body="B",
                relations=[
                    {"src": "war", "dst": "copper", "kind": "affects"},
                    {"src": "copper", "dst": "semiconductor", "kind": "uses"},
                    {"src": "helium", "dst": "semiconductor", "kind": "enables"},
                ],
            ),
        ]
        for note in notes:
            store.write_note(note)
        return store

    def test_declared_edges_aggregate_support_and_render_as_virtual_concepts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._store_with_relations(td)
            graph = ConceptGraphBuilder(store).build_for_session("").to_dict()
            store.close()

        nodes = {node["id"]: node for node in graph["nodes"]}
        edges = {edge["id"]: edge for edge in graph["edges"]}
        self.assertEqual(
            set(nodes),
            {"concept:war", "concept:helium", "concept:copper", "concept:semiconductor"},
        )
        self.assertTrue(all(node["virtual"] and node["kind"] == "concept" for node in nodes.values()))
        self.assertTrue(all(node["weight"] >= 2.6 for node in nodes.values()))
        war_helium = edges["concept:war->concept:helium:affects"]
        self.assertEqual(war_helium["label"], "affects (2)")
        self.assertEqual(war_helium["weight"], 2.0)
        self.assertIn("concept:copper->concept:semiconductor:uses", edges)

    def test_missing_links_are_node_text_not_edges(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._store_with_relations(td)
            graph = ConceptGraphBuilder(store).build_for_session("").to_dict()
            store.close()

        nodes = {node["id"]: node for node in graph["nodes"]}
        edge_pairs = {frozenset((edge["src"], edge["dst"])) for edge in graph["edges"]}
        # helium and copper share war + semiconductor but have no declared edge.
        self.assertNotIn(frozenset(("concept:helium", "concept:copper")), edge_pairs)
        excerpt = nodes["concept:helium"]["excerpt"]
        self.assertIn("Unproven; not facts.", excerpt)
        self.assertIn("copper", excerpt)
        self.assertIn("shared neighbor:", excerpt)
        suggestion = next(
            line for line in excerpt.splitlines() if "shared neighbor:" in line
        )
        self.assertIn(suggestion, nodes["concept:copper"]["excerpt"])

    def test_concept_excerpt_groups_relations_with_supporting_titles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            note = KnowledgeNote.create(
                type="note", title="Helium supply shock", body="B",
                relations=[{"src": "war", "dst": "helium supply", "kind": "affects"}],
            )
            incoming = KnowledgeNote.create(
                type="note", title="Chipmaking noble gases", body="B",
                relations=[
                    {"src": "semiconductor manufacturing", "dst": "helium supply", "kind": "uses"}
                ],
            )
            store.write_note(note)
            store.write_note(incoming)
            graph = ConceptGraphBuilder(store).build_for_session("").to_dict()
            store.close()

        nodes = {node["id"]: node for node in graph["nodes"]}
        war_excerpt = nodes["concept:war"]["excerpt"]
        helium_excerpt = nodes["concept:helium supply"]["excerpt"]
        self.assertIn("## Declared Relations", war_excerpt)
        self.assertIn("## Outgoing", war_excerpt)
        self.assertIn("- war --affects--> helium supply", war_excerpt)
        self.assertIn("1 supporting note: Helium supply shock", war_excerpt)
        self.assertNotIn(note.id, war_excerpt)
        self.assertIn("## Incoming", helium_excerpt)
        self.assertIn("- semiconductor manufacturing --uses--> helium supply", helium_excerpt)
        self.assertIn("1 supporting note: Chipmaking noble gases", helium_excerpt)

    def test_node_and_edge_limits_bound_synthesis_appends(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            for i in range(9):
                store.write_note(
                    KnowledgeNote.create(
                        type="note", title=f"C{i}", body="B",
                        relations=[{"src": f"topic {i}", "dst": f"topic {i + 1}", "kind": "relates"}],
                    )
                )
            for i in range(4):
                store.write_note(
                    KnowledgeNote.create(
                        type="synthesis", title=f"S{i}", body="S",
                        tags=[f"topic {i}", f"topic {i + 1}"],
                    )
                )
            artifact = ConceptGraphBuilder(store).build_for_session(
                "", node_limit=8, edge_limit=8
            )
            graph = artifact.to_dict()
            store.close()

        # Synthesis appends must not blow past the requested limits.
        self.assertLessEqual(len(graph["nodes"]), 8)
        self.assertLessEqual(len(graph["edges"]), 8)
        self.assertTrue(all(node["kind"] == "concept" for node in graph["nodes"]))

    def test_declared_edges_keep_visible_endpoints_after_pair_selection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            for i in range(8):
                store.write_note(
                    KnowledgeNote.create(
                        type="note",
                        title=f"A{i}",
                        body="B",
                        relations=[{"src": f"topic {i}", "dst": f"topic {(i + 1) % 8}", "kind": "relates"}],
                    )
                )
            for i in range(2):
                store.write_note(
                    KnowledgeNote.create(
                        type="note",
                        title=f"Z{i}",
                        body="B",
                        relations=[{"src": "zzz source", "dst": "zzz target", "kind": "affects"}],
                    )
                )

            graph = ConceptGraphBuilder(store).build_for_session(
                "", node_limit=8, edge_limit=8
            ).to_dict()
            store.close()

        self.assertGreater(len(graph["edges"]), 0)
        self.assertLessEqual(len(graph["edges"]), 8)
        node_ids = {node["id"] for node in graph["nodes"]}
        self.assertTrue(all(edge["src"] in node_ids and edge["dst"] in node_ids for edge in graph["edges"]))

    def test_relation_selection_keeps_endpoint_pairs_before_filling_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            for i in range(80):
                store.write_note(
                    KnowledgeNote.create(
                        type="note",
                        title=f"War helium {i}",
                        body="B",
                        relations=[
                            {
                                "src": f"war topic {i:02d}",
                                "dst": f"helium topic {i:02d}",
                                "kind": "affects",
                            }
                        ],
                    )
                )

            graph = ConceptGraphBuilder(store).build_for_session(
                "", node_limit=64, edge_limit=128
            ).to_dict()
            store.close()

        node_ids = {node["id"] for node in graph["nodes"]}
        self.assertEqual(len(graph["nodes"]), 64)
        self.assertEqual(len(graph["edges"]), 32)
        self.assertTrue(all(edge["src"] in node_ids and edge["dst"] in node_ids for edge in graph["edges"]))

    def test_current_session_relation_edges_beat_old_high_support_edges(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            store.write_note(
                KnowledgeNote.create(
                    type="note",
                    title="Current relationship",
                    body="B",
                    tags=["current a", "current b"],
                    relations=[{"src": "current a", "dst": "current b", "kind": "affects"}],
                    session_id="s1",
                )
            )
            for i in range(12):
                for support in range(3):
                    store.write_note(
                        KnowledgeNote.create(
                            type="note",
                            title=f"Old relationship {i}-{support}",
                            body="B",
                            tags=[f"old a {i:02d}", f"old b {i:02d}"],
                            relations=[
                                {
                                    "src": f"old a {i:02d}",
                                    "dst": f"old b {i:02d}",
                                    "kind": "affects",
                                }
                            ],
                            session_id="old",
                        )
                    )

            graph = ConceptGraphBuilder(store).build_for_session(
                "s1",
                node_limit=24,
                edge_limit=8,
            ).to_dict()
            store.close()

        node_ids = {node["id"] for node in graph["nodes"]}
        edge_ids = {edge["id"] for edge in graph["edges"]}
        self.assertIn("concept:current a", node_ids)
        self.assertIn("concept:current b", node_ids)
        self.assertIn("concept:current a->concept:current b:affects", edge_ids)
        self.assertLessEqual(len(graph["edges"]), 8)

    def test_shared_concept_old_edges_do_not_crowd_out_current_relation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            store.write_note(
                KnowledgeNote.create(
                    type="note",
                    title="Current war helium",
                    body="B",
                    tags=["war", "helium"],
                    relations=[{"src": "war", "dst": "helium", "kind": "affects"}],
                    session_id="s1",
                )
            )
            for i in range(12):
                for support in range(3):
                    store.write_note(
                        KnowledgeNote.create(
                            type="note",
                            title=f"Old war topic {i}-{support}",
                            body="B",
                            tags=["war", f"old topic {i:02d}"],
                            relations=[
                                {
                                    "src": "war",
                                    "dst": f"old topic {i:02d}",
                                    "kind": "affects",
                                }
                            ],
                            session_id="old",
                        )
                    )

            graph = ConceptGraphBuilder(store).build_for_session(
                "s1",
                node_limit=24,
                edge_limit=8,
            ).to_dict()
            store.close()

        node_ids = {node["id"] for node in graph["nodes"]}
        nodes = {node["id"]: node for node in graph["nodes"]}
        edge_ids = {edge["id"] for edge in graph["edges"]}
        self.assertIn("concept:war", node_ids)
        self.assertIn("concept:helium", node_ids)
        self.assertIn("concept:war->concept:helium:affects", edge_ids)
        self.assertIn("- war --affects--> helium", nodes["concept:war"]["excerpt"])
        self.assertLessEqual(len(graph["edges"]), 8)

    def test_builder_limit_args_are_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            store.write_note(
                KnowledgeNote.create(
                    type="note",
                    title="A",
                    body="B",
                    relations=[{"src": "war", "dst": "helium", "kind": "affects"}],
                )
            )

            graph = ConceptGraphBuilder(store).build_for_session(
                "", node_limit="bad", edge_limit=None
            ).to_dict()
            store.close()

        self.assertEqual({node["id"] for node in graph["nodes"]}, {"concept:war", "concept:helium"})
        self.assertEqual(len(graph["edges"]), 1)

    def test_non_active_notes_do_not_feed_concept_graph(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            store.write_note(
                KnowledgeNote.create(
                    type="note", title="Old", body="B", status="contradicted",
                    tags=["gold"],
                    relations=[{"src": "war", "dst": "helium", "kind": "affects"}],
                )
            )
            store.write_note(
                KnowledgeNote.create(
                    type="note", title="Live", body="B",
                    relations=[{"src": "war", "dst": "copper", "kind": "affects"}],
                )
            )
            graph = ConceptGraphBuilder(store).build_for_session("").to_dict()
            store.close()

        node_ids = {node["id"] for node in graph["nodes"]}
        self.assertEqual(node_ids, {"concept:war", "concept:copper"})
        self.assertEqual(len(graph["edges"]), 1)

    def test_co_tags_never_create_concept_edges(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            for title in ("A", "B"):
                store.write_note(
                    KnowledgeNote.create(type="note", title=title, body="B", tags=["gold", "oil"])
                )
            graph = ConceptGraphBuilder(store).build_for_session("").to_dict()
            store.close()

        node_ids = {node["id"] for node in graph["nodes"]}
        self.assertEqual(node_ids, {"concept:gold", "concept:oil"})
        self.assertEqual(graph["edges"], [])

    def test_synthesis_notes_attach_to_concepts_and_session_focus(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = self._store_with_relations(td)
            synthesis = KnowledgeNote.create(
                type="synthesis",
                title="Helium report",
                body="S",
                tags=["research", "session:s1", "helium", "war"],
                session_id="s1",
            )
            store.write_note(synthesis)
            graph = ConceptGraphBuilder(store).build_for_session("s1").to_dict()
            store.close()

        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(nodes[synthesis.id]["kind"], "synthesis")
        self.assertFalse(nodes[synthesis.id]["virtual"])
        tagged = [edge for edge in graph["edges"] if edge["kind"] == "tagged"]
        self.assertEqual(
            {edge["dst"] for edge in tagged if edge["src"] == synthesis.id},
            {"concept:helium", "concept:war"},
        )
        self.assertTrue(nodes["concept:helium"]["focus"])
        self.assertFalse(nodes["concept:semiconductor"]["focus"])
        self.assertIn(graph["center_id"], set(graph["focus_ids"]))

    def test_empty_vault_returns_warning_not_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            graph = ConceptGraphBuilder(store).build_for_session("s1").to_dict()
            store.close()

        self.assertEqual(graph["nodes"], [])
        self.assertEqual(graph["edges"], [])
        self.assertTrue(any("No concepts yet" in warning for warning in graph["warnings"]))


class UnifiedResearchGraphBuilderTests(unittest.TestCase):
    def test_unified_graph_layers_concepts_report_notes_and_sources_by_depth(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            synthesis = KnowledgeNote.create(
                type="synthesis",
                title="War metals report",
                body=(
                    "## 结论\n"
                    "War affects aluminum.\n\n"
                    "## 来源\n"
                    "[1] Aluminum News - https://example.com/aluminum\n"
                ),
                tags=["war", "aluminum"],
                sources=["https://example.com/aluminum"],
                session_id="s1",
            )
            fact = KnowledgeNote.create(
                type="fact",
                title="Aluminum supply shock",
                body="## Claim\nAluminum supply was disrupted.",
                tags=["war", "aluminum supply"],
                sources=["https://example.com/aluminum"],
                relations=[{"src": "war", "dst": "aluminum supply", "kind": "affects"}],
                session_id="s1",
            )
            store.write_note(synthesis)
            store.write_note(fact)
            store.link(synthesis.id, fact.id, "derives")

            depth_one = UnifiedResearchGraphBuilder(store).build_for_session(
                "s1",
                focus_ids=(synthesis.id,),
                depth=1,
            ).to_dict()
            depth_two = UnifiedResearchGraphBuilder(store).build_for_session(
                "s1",
                focus_ids=(synthesis.id,),
                depth=2,
            ).to_dict()
            depth_three = UnifiedResearchGraphBuilder(store).build_for_session(
                "s1",
                focus_ids=(synthesis.id,),
                depth=3,
            ).to_dict()
            store.close()

        one_ids = {node["id"] for node in depth_one["nodes"]}
        two_ids = {node["id"] for node in depth_two["nodes"]}
        three_ids = {node["id"] for node in depth_three["nodes"]}
        self.assertIn("concept:war", one_ids)
        self.assertIn("concept:aluminum", one_ids)
        self.assertIn(synthesis.id, one_ids)
        self.assertNotIn(fact.id, one_ids)
        self.assertFalse(any(node["kind"] == "source_url" for node in depth_one["nodes"]))

        self.assertIn(fact.id, two_ids)
        self.assertFalse(any(node["kind"] == "source_url" for node in depth_two["nodes"]))
        self.assertTrue(any(edge["kind"] == "tagged" for edge in depth_two["edges"]))
        self.assertTrue(any(edge["kind"] == "affects" for edge in depth_two["edges"]))

        self.assertIn(fact.id, three_ids)
        source = next(node for node in depth_three["nodes"] if node["kind"] == "source_url")
        self.assertEqual(source["label"], "Aluminum News")
        self.assertIn("https://example.com/aluminum", source["excerpt"])
        self.assertIn(source["id"], three_ids)
        self.assertTrue(any(edge["kind"] == "cites" for edge in depth_three["edges"]))

    def test_unified_graph_limits_stay_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            synthesis = KnowledgeNote.create(
                type="synthesis",
                title="Many concepts",
                body="S",
                tags=["topic 0"],
                session_id="s1",
            )
            store.write_note(synthesis)
            for i in range(20):
                store.write_note(
                    KnowledgeNote.create(
                        type="fact",
                        title=f"Fact {i}",
                        body="B",
                        tags=[f"topic {i}"],
                        relations=[{"src": f"topic {i}", "dst": f"topic {i + 1}", "kind": "relates"}],
                        session_id="s1",
                    )
                )

            graph = UnifiedResearchGraphBuilder(store).build_for_session(
                "s1",
                focus_ids=(synthesis.id,),
                depth=3,
                node_limit=8,
                edge_limit=8,
            ).to_dict()
            store.close()

        self.assertLessEqual(len(graph["nodes"]), 8)
        self.assertLessEqual(len(graph["edges"]), 8)

    def test_unified_graph_preserves_current_evidence_spine_when_global_concepts_are_many(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            synthesis = KnowledgeNote.create(
                type="synthesis",
                title="Current report",
                body="## 来源\n[1] Current Source - https://example.com/current\n",
                tags=["current topic"],
                sources=["https://example.com/current"],
                session_id="current",
            )
            fact = KnowledgeNote.create(
                type="fact",
                title="Current fact",
                body="Current evidence",
                tags=["current topic", "current fact"],
                sources=["https://example.com/current"],
                relations=[{"src": "current topic", "dst": "current fact", "kind": "supports"}],
                session_id="current",
            )
            store.write_note(synthesis)
            store.write_note(fact)
            store.link(synthesis.id, fact.id, "derives")
            for i in range(140):
                store.write_note(
                    KnowledgeNote.create(
                        type="fact",
                        title=f"Global concept {i}",
                        body="Global",
                        relations=[
                            {
                                "src": f"global concept {i}",
                                "dst": f"global neighbor {i}",
                                "kind": "relates",
                            }
                        ],
                        session_id="old",
                    )
                )

            graph = UnifiedResearchGraphBuilder(store).build_for_session(
                "current",
                focus_ids=(synthesis.id,),
                depth=3,
            ).to_dict()
            store.close()

        node_ids = {node["id"] for node in graph["nodes"]}
        self.assertIn(synthesis.id, node_ids)
        self.assertIn(fact.id, node_ids)
        source = next(node for node in graph["nodes"] if node["kind"] == "source_url")
        self.assertEqual(source["label"], "Current Source")
        self.assertLessEqual(len(graph["nodes"]), 96)
        self.assertTrue(any(edge["src"] == synthesis.id and edge["dst"] == fact.id for edge in graph["edges"]))
        self.assertTrue(any(edge["src"] == fact.id and edge["kind"] == "cites" for edge in graph["edges"]))


if __name__ == "__main__":
    unittest.main()
