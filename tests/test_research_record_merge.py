from __future__ import annotations

import tempfile
from pathlib import Path

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.research.done_finalizer import finalize_done_answer
from codey.research.object_model import build_research_record
from codey.research.plan_executor import PlanExecutionResult
from codey.research.record_merge import merge_evidence_patch
from codey.research.runner import ResearchRunResult
from codey.research.source_document import SourceDocument
from codey.research.tools import ResearchTools


class _DummySearch:
    def close(self) -> None:
        pass


def test_merge_evidence_patch_appends_new_evidence_and_reindexes_citations() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        try:
            tools = ResearchTools(
                search=_DummySearch(),
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-merge",
                project="project-merge",
            )
            # 1. Setup initial source and evidence
            tools.sources_read.add("https://example.com/initial")
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url="https://example.com/initial",
                final_url="https://example.com/initial",
                title="Initial Source",
                text="Initial evidence text excerpt.",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Initial Claim",
                "body": "Initial description",
                "sources": ["https://example.com/initial"],
                "evidence": [{
                    "source_url": "https://example.com/initial",
                    "excerpt": "Initial evidence text",
                    "claim": "Initial Claim",
                }],
            })

            initial_summary = (
                "## 结论\nInitial summary of findings [1].\n\n"
                "## 关键证据\n- [1] Initial Claim: Initial evidence text.\n\n"
                "## 来源\n[1] Initial Source - https://example.com/initial"
            )
            initial_finalized = finalize_done_answer(initial_summary, tools.ledger)
            initial_record = build_research_record(
                summary=initial_finalized.text,
                question="What are the findings?",
                session_id="session-merge",
                project="project-merge",
                run_id="run-merge",
                ledger=tools.ledger,
                stop_reason="done",
            )
            initial_result = ResearchRunResult(
                question="What are the findings?",
                summary=initial_finalized.text,
                stop_reason="done",
                turns=2,
                max_turns_used=2,
                research_record=initial_record,
            )

            # 2. Add follow-up fresh source and evidence
            tools.sources_read.add("https://example.com/followup")
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url="https://example.com/followup",
                final_url="https://example.com/followup",
                title="Followup Source",
                text="Followup evidence text excerpt with limitation notes.",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Followup Limitation Claim",
                "body": "Limitation description",
                "sources": ["https://example.com/followup"],
                "evidence": [{
                    "source_url": "https://example.com/followup",
                    "excerpt": "Followup evidence text",
                    "claim": "Followup Limitation Claim",
                    "stance": "context",
                }],
            })

            material = PlanExecutionResult(
                fresh_source_urls=("https://example.com/followup",),
            )

            # 3. Perform deterministic merge
            merged_result = merge_evidence_patch(initial_result, tools, material)

            assert merged_result.turns == 3
            assert merged_result.stop_reason == "done"
            assert "https://example.com/followup" in merged_result.summary
            assert "## 来源" in merged_result.summary
            assert "[1] Initial Source" in merged_result.summary
            assert "[2] Followup Source" in merged_result.summary
            assert merged_result.research_record is not None
            assert len(merged_result.research_record.sources) == 2
            assert len(merged_result.research_record.evidence) == 2
            assert merged_result.synthesis_id.startswith("synthesis:merge:")
            assert len(merged_result.source_urls) == 2
            assert merged_result.sources_read == 2
            assert len(merged_result.evidence_items) == 2
            assert len(merged_result.notes_created) == 2
            assert len(merged_result.counterpoints) == 1
            assert "Followup Limitation Claim" in merged_result.counterpoints[0]

            # 4. Merging again with same inputs is idempotent
            merged_again = merge_evidence_patch(merged_result, tools, material)
            assert merged_again.summary == merged_result.summary
            assert merged_again.research_record.record_digest == merged_result.research_record.record_digest
        finally:
            store.index.close()


def test_merge_evidence_patch_prunes_unsupported_raw_claims() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        try:
            tools = ResearchTools(
                search=_DummySearch(),
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-prune",
                project="project-prune",
            )
            tools.sources_read.add("https://example.com/source1")
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url="https://example.com/source1",
                final_url="https://example.com/source1",
                title="Source 1",
                text="Verified text excerpt.",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Claim 1",
                "body": "Body",
                "sources": ["https://example.com/source1"],
                "evidence": [{
                    "source_url": "https://example.com/source1",
                    "excerpt": "Verified text excerpt.",
                    "claim": "Claim 1",
                }],
            })
            # Initial summary has unsupported overclaim lines across conclusion, evidence, and counter
            initial_summary = (
                "## 结论\n"
                "Verified finding [1].\n"
                "Hallucinated conclusion line with no citation bracket at all.\n\n"
                "## 关键证据\n"
                "- [1] Claim 1.\n"
                "- Unsupported hallucinated claim without any citation bracket.\n\n"
                "## 反证与限制\n"
                "- Unsupported counter claim with no evidence or marker.\n\n"
                "## 来源\n[1] Source 1 - https://example.com/source1"
            )
            initial_finalized = finalize_done_answer(initial_summary, tools.ledger)
            initial_record = build_research_record(
                summary=initial_finalized.text,
                question="What is the truth?",
                session_id="session-prune",
                ledger=tools.ledger,
                stop_reason="done",
            )
            initial_result = ResearchRunResult(
                question="What is the truth?",
                summary=initial_finalized.text,
                stop_reason="done",
                turns=1,
                research_record=initial_record,
            )

            # Fresh source
            tools.sources_read.add("https://example.com/source2")
            tools.ledger.record_search("truth query", [{
                "title": "Source 2",
                "url": "https://example.com/source2",
                "snippet": "Second verified fact snippet.",
            }])
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url="https://example.com/source2",
                final_url="https://example.com/source2",
                title="Source 2",
                text="Second verified fact.",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Claim 2",
                "body": "Body 2",
                "sources": ["https://example.com/source2"],
                "evidence": [{
                    "source_url": "https://example.com/source2",
                    "excerpt": "Second verified fact.",
                    "claim": "Claim 2",
                }],
            })
            material = PlanExecutionResult(
                queries_executed=("truth query",),
                fresh_source_urls=("https://example.com/source2",),
            )
            merged = merge_evidence_patch(initial_result, tools, material)

            # All unsupported hallucinated lines in conclusion/evidence/counter are pruned
            assert "Hallucinated conclusion line" not in merged.summary
            assert "Unsupported hallucinated claim" not in merged.summary
            assert "Unsupported counter claim" not in merged.summary
            assert "[1] Source 1" in merged.summary
            assert "[2] Source 2" in merged.summary

            # Search results preserve full payload structure
            assert len(merged.search_results) == 1
            sr = merged.search_results[0]
            assert sr["query"] == "truth query"
            assert sr["url"] == "https://example.com/source2"
            assert sr["opened"] is True
            assert sr["final_url"] == "https://example.com/source2"
        finally:
            store.index.close()




def test_merge_evidence_patch_preserves_same_excerpt_from_different_sources() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        try:
            tools = ResearchTools(
                search=_DummySearch(),
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-merge2",
                project="project-merge2",
            )
            # Source 1 with quote
            tools.sources_read.add("https://example.com/src1")
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url="https://example.com/src1",
                final_url="https://example.com/src1",
                title="Source 1",
                text="Identical quote text from official release.",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Claim 1",
                "body": "Body",
                "sources": ["https://example.com/src1"],
                "evidence": [{
                    "source_url": "https://example.com/src1",
                    "excerpt": "Identical quote text from official release.",
                    "claim": "Claim 1",
                }],
            })
            initial_summary = "## 结论\nFinding [1].\n\n## 关键证据\n- [1] Finding.\n\n## 来源\n[1] Source 1 - https://example.com/src1"
            initial_finalized = finalize_done_answer(initial_summary, tools.ledger)
            initial_record = build_research_record(
                summary=initial_finalized.text,
                question="What happened?",
                session_id="session-merge2",
                ledger=tools.ledger,
                stop_reason="done",
            )
            initial_result = ResearchRunResult(
                question="What happened?",
                summary=initial_finalized.text,
                stop_reason="done",
                turns=1,
                research_record=initial_record,
            )

            # Source 2 has exact identical excerpt string, but from a different source
            tools.sources_read.add("https://example.com/src2")
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url="https://example.com/src2",
                final_url="https://example.com/src2",
                title="Source 2",
                text="Identical quote text from official release.",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Claim 2",
                "body": "Body 2",
                "sources": ["https://example.com/src2"],
                "evidence": [{
                    "source_url": "https://example.com/src2",
                    "excerpt": "Identical quote text from official release.",
                    "claim": "Claim 2",
                }],
            })
            material = PlanExecutionResult(fresh_source_urls=("https://example.com/src2",))

            merged = merge_evidence_patch(initial_result, tools, material)
            assert len(merged.research_record.evidence) == 2
            assert len(merged.research_record.sources) == 2
            assert "https://example.com/src1" in merged.summary
            assert "https://example.com/src2" in merged.summary
        finally:
            store.index.close()


def test_merge_evidence_patch_handles_unparseable_or_protocol_initial_summary() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        try:
            tools = ResearchTools(
                search=_DummySearch(),
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-merge3",
                project="project-merge3",
            )
            tools.sources_read.add("https://example.com/fresh")
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url="https://example.com/fresh",
                final_url="https://example.com/fresh",
                title="Fresh Doc",
                text="Verified recovery fact.",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Recovery Fact",
                "body": "Recovery body",
                "sources": ["https://example.com/fresh"],
                "evidence": [{
                    "source_url": "https://example.com/fresh",
                    "excerpt": "Verified recovery fact.",
                    "claim": "Recovery Fact",
                }],
            })
            # Initial summary has no markdown headings / is a protocol error
            initial_result = ResearchRunResult(
                question="What is the answer?",
                summary="ERROR: Protocol violation in initial step",
                stop_reason="protocol_violation",
                turns=1,
            )
            material = PlanExecutionResult(fresh_source_urls=("https://example.com/fresh",))

            merged = merge_evidence_patch(initial_result, tools, material)
            assert merged.stop_reason == "done"
            assert "## 结论" in merged.summary
            assert "## 关键证据" in merged.summary
            assert "https://example.com/fresh" in merged.summary
            assert len(merged.research_record.evidence) == 1
        finally:
            store.index.close()
