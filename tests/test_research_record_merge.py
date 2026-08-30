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
            assert "path" not in initial_record.project_ref
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

            assert merged_result.turns == 2
            assert merged_result.max_turns_used == 2
            assert merged_result.stop_reason == "done"
            assert "https://example.com/followup" in merged_result.summary
            assert "## 来源" in merged_result.summary
            assert "[1] Initial Source" in merged_result.summary
            assert "[2] Followup Source" in merged_result.summary
            assert merged_result.research_record is not None
            assert merged_result.research_record.project_ref == initial_record.project_ref
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
            # Initial summary has unsupported overclaim lines across conclusion, evidence, and counter,
            # including un-cited lines and unmapped/dangling citation numbers like [99]
            initial_summary = (
                "## 结论\n"
                "Verified finding [1].\n"
                "Hallucinated conclusion line with no citation bracket at all.\n"
                "Dangling conclusion line with unmapped citation [99].\n\n"
                "## 关键证据\n"
                "- [1] Claim 1.\n"
                "- Unsupported hallucinated claim without any citation bracket.\n"
                "- [99] Dangling evidence line with unmapped citation.\n\n"
                "## 反证与限制\n"
                "- Unsupported counter claim with no evidence or marker.\n"
                "- [99] Dangling counter claim with unmapped citation.\n\n"
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
                summary=initial_summary,
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

            # All unsupported hallucinated lines in conclusion/evidence/counter (and dangling [99]) are pruned
            assert "Hallucinated conclusion line" not in merged.summary
            assert "Dangling conclusion line" not in merged.summary
            assert "Unsupported hallucinated claim" not in merged.summary
            assert "Dangling evidence line" not in merged.summary
            assert "Unsupported counter claim" not in merged.summary
            assert "Dangling counter claim" not in merged.summary
            assert "[99]" not in merged.summary
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


def test_merge_evidence_patch_preserves_markdown_link_source_citations() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        try:
            tools = ResearchTools(
                search=_DummySearch(),
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-markdown-citation",
                project="project-markdown-citation",
            )
            source_a = "https://example.com/source-a"
            source_b = "https://example.com/source-b"
            tools.sources_read.add(source_a)
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url=source_a,
                final_url=source_a,
                title="Source A",
                text="Source A verified fact.",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Source A Fact",
                "body": "Source A verified fact.",
                "sources": [source_a],
                "evidence": [{
                    "source_url": source_a,
                    "excerpt": "Source A verified fact.",
                    "claim": "Source A verified fact.",
                }],
            })
            initial_summary = (
                "## 结论\n"
                "Source A verified fact [1].\n\n"
                "## 关键证据\n"
                "- [1] Source A verified fact.\n\n"
                "## 反证与限制\n"
                "- 未找到强反证。\n\n"
                "## 来源质量\n"
                "- [1] Source A: primary.\n\n"
                "## 搜索覆盖\n"
                "- 查询: source a.\n\n"
                "## 来源\n"
                "[1] [Source A](https://example.com/source-a)"
            )
            initial_record = build_research_record(
                summary=initial_summary,
                question="What facts are supported?",
                session_id="session-markdown-citation",
                project="project-markdown-citation",
                run_id="run-markdown-citation",
                ledger=tools.ledger,
                stop_reason="done",
            )
            initial_result = ResearchRunResult(
                question="What facts are supported?",
                summary=initial_summary,
                stop_reason="done",
                turns=2,
                max_turns_used=2,
                research_record=initial_record,
            )

            tools.sources_read.add(source_b)
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url=source_b,
                final_url=source_b,
                title="Source B",
                text="Source B verified follow-up fact.",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Source B Fact",
                "body": "Source B verified follow-up fact.",
                "sources": [source_b],
                "evidence": [{
                    "source_url": source_b,
                    "excerpt": "Source B verified follow-up fact.",
                    "claim": "Source B verified follow-up fact.",
                }],
            })
            material = PlanExecutionResult(fresh_source_urls=(source_b,))

            merged = merge_evidence_patch(initial_result, tools, material)

            assert "Source A verified fact" in merged.summary
            assert "Source B verified follow-up fact" in merged.summary
            assert source_a in merged.summary
            assert source_b in merged.summary
            assert len(merged.research_record.evidence) == 2
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


def test_merge_evidence_patch_builds_minimal_candidate_from_protocol_result_with_ledger_evidence() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        try:
            tools = ResearchTools(
                search=_DummySearch(),
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-stepfun",
                project="project-stepfun",
            )
            source_a = "https://source-a.test/widget-storage"
            source_b = "https://source-b.test/widget-storage-update"
            tools.ledger.record_search("current Widget Storage API recommendation endpoint", [{
                "title": "Benchmark source A",
                "url": source_a,
                "snippet": "The stable-v2 endpoint is still recommended.",
            }])
            tools.sources_read.add(source_a)
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url=source_a,
                final_url=source_a,
                title="Benchmark source A",
                text="The Widget Storage standard still recommends the stable-v2 endpoint.",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Initial Widget Storage endpoint",
                "body": "The Widget Storage standard still recommends stable-v2.",
                "sources": [source_a],
                "evidence": [{
                    "source_url": source_a,
                    "excerpt": "The Widget Storage standard still recommends the stable-v2 endpoint.",
                    "claim": "The Widget Storage standard still recommends the stable-v2 endpoint.",
                }],
            })
            initial_record = build_research_record(
                summary="",
                question="What is the current Widget Storage API recommendation endpoint?",
                session_id="session-stepfun",
                project="project-stepfun",
                run_id="run-stepfun",
                ledger=tools.ledger,
                stop_reason="protocol",
            )
            assert initial_record.answer_status == "not_answered"
            initial_result = ResearchRunResult(
                question="What is the current Widget Storage API recommendation endpoint?",
                summary="",
                stop_reason="protocol",
                turns=7,
                max_turns_used=14,
                research_record=initial_record,
                evidence_items=tools.ledger.evidence_payload(),
                opened_sources=tools.ledger.opened_sources_payload(),
                coverage=tools.ledger.coverage_payload(),
            )

            tools.ledger.record_search("current primary source evidence", [{
                "title": "Benchmark source B",
                "url": source_b,
                "snippet": "After the 2026 update, stable-v3 is recommended.",
            }])
            tools.sources_read.add(source_b)
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url=source_b,
                final_url=source_b,
                title="Benchmark source B",
                text="After the May 2026 update, Widget Storage recommends the stable-v3 endpoint.",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Updated Widget Storage endpoint",
                "body": "After the May 2026 update, Widget Storage recommends stable-v3.",
                "sources": [source_b],
                "evidence": [{
                    "source_url": source_b,
                    "excerpt": "After the May 2026 update, Widget Storage recommends the stable-v3 endpoint.",
                    "claim": "After the May 2026 update, Widget Storage recommends the stable-v3 endpoint.",
                }],
            })
            material = PlanExecutionResult(
                queries_executed=("current primary source evidence",),
                fresh_source_urls=(source_b,),
            )

            merged = merge_evidence_patch(initial_result, tools, material)

            assert merged.stop_reason == "done"
            assert merged.research_record is not None
            assert merged.research_record.answer_status in {"answered", "partial"}
            assert merged.research_record.unsupported_claim_count == 0
            assert len(merged.research_record.sources) == 2
            assert len(merged.research_record.evidence) == 2
            assert len(merged.research_record.claims) >= 2
            assert len(merged.citation_map) == 2
            assert source_a in merged.summary
            assert source_b in merged.summary
            assert "stable-v3" in merged.summary
            assert "## 来源质量" in merged.summary
            assert "## 搜索覆盖" in merged.summary
            assert "ERROR:" not in merged.summary
            assert merged.coverage["opened_count"] == 2
            assert len(merged.search_results) == 2
        finally:
            store.index.close()


def test_merge_evidence_patch_rebuilds_when_pruning_leaves_no_supported_body_lines() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        try:
            tools = ResearchTools(
                search=_DummySearch(),
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-pruned-rebuild",
                project="project-pruned-rebuild",
            )
            source_a = "https://example.com/source-a"
            source_b = "https://example.com/source-b"
            tools.ledger.record_search("primary evidence", [{
                "title": "Source A",
                "url": source_a,
                "snippet": "Source A verified fact.",
            }])
            tools.sources_read.add(source_a)
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url=source_a,
                final_url=source_a,
                title="Source A",
                text="Source A verified fact.",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Source A Fact",
                "body": "Source A verified fact.",
                "sources": [source_a],
                "evidence": [{
                    "source_url": source_a,
                    "excerpt": "Source A verified fact.",
                    "claim": "Source A verified fact.",
                }],
            })
            hallucinated_summary = (
                "## 结论\n"
                "Uncited invented conclusion.\n"
                "Dangling invented conclusion [99].\n\n"
                "## 关键证据\n"
                "- Uncited invented evidence.\n"
                "- [99] Dangling invented evidence.\n\n"
                "## 反证与限制\n"
                "- Uncited invented limitation.\n\n"
                "## 来源质量\n"
                "- invented quality text.\n\n"
                "## 搜索覆盖\n"
                "- invented coverage text.\n\n"
                "## 来源\n"
                "[1] Source A - https://example.com/source-a"
            )
            initial_record = build_research_record(
                summary=hallucinated_summary,
                question="What facts are supported?",
                session_id="session-pruned-rebuild",
                project="project-pruned-rebuild",
                run_id="run-pruned-rebuild",
                ledger=tools.ledger,
                stop_reason="done",
            )
            assert initial_record.answer_status == "partial"
            initial_result = ResearchRunResult(
                question="What facts are supported?",
                summary=hallucinated_summary,
                stop_reason="done",
                turns=2,
                research_record=initial_record,
            )

            tools.ledger.record_search("fresh evidence", [{
                "title": "Source B",
                "url": source_b,
                "snippet": "Source B verified follow-up fact.",
            }])
            tools.sources_read.add(source_b)
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url=source_b,
                final_url=source_b,
                title="Source B",
                text="Source B verified follow-up fact.",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Source B Fact",
                "body": "Source B verified follow-up fact.",
                "sources": [source_b],
                "evidence": [{
                    "source_url": source_b,
                    "excerpt": "Source B verified follow-up fact.",
                    "claim": "Source B verified follow-up fact.",
                }],
            })
            material = PlanExecutionResult(
                queries_executed=("fresh evidence",),
                fresh_source_urls=(source_b,),
            )

            merged = merge_evidence_patch(initial_result, tools, material)

            assert merged.stop_reason == "done"
            assert "Uncited invented" not in merged.summary
            assert "Dangling invented" not in merged.summary
            assert "Research findings supported" not in merged.summary
            assert "Source A verified fact" in merged.summary
            assert "Source B verified follow-up fact" in merged.summary
            assert "- 查询:" in merged.summary
            assert "- 已打开来源: 2; 证据条目: 2" in merged.summary
            assert len(merged.research_record.evidence) == 2
            assert merged.research_record.unsupported_claim_count == 0
        finally:
            store.index.close()


def test_merge_evidence_patch_does_not_fabricate_counterclaim_or_conclusion() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        try:
            tools = ResearchTools(
                search=_DummySearch(),
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-no-auto",
                project="project-no-auto",
            )
            initial_url = "https://example.com/initial-auto"
            followup_url = "https://example.com/followup-auto"
            tools.sources_read.add(initial_url)
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url=initial_url,
                final_url=initial_url,
                title="Initial Auto Source",
                text="Initial source verified fact.",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Initial Auto Fact",
                "body": "Initial source verified fact.",
                "sources": [initial_url],
                "evidence": [{
                    "source_url": initial_url,
                    "excerpt": "Initial source verified fact.",
                    "claim": "Initial source verified fact.",
                }],
            })
            initial_summary = (
                "## 结论\n\n"
                "## 关键证据\n"
                "- [1] Initial source verified fact.\n\n"
                "## 反证与限制\n\n"
                "## 来源\n"
                f"[1] Initial Auto Source - {initial_url}"
            )
            initial_record = build_research_record(
                summary=initial_summary,
                question="What facts are supported?",
                session_id="session-no-auto",
                project="project-no-auto",
                run_id="run-no-auto",
                ledger=tools.ledger,
                stop_reason="done",
            )
            initial_result = ResearchRunResult(
                question="What facts are supported?",
                summary=initial_summary,
                stop_reason="done",
                turns=2,
                research_record=initial_record,
            )

            tools.sources_read.add(followup_url)
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url=followup_url,
                final_url=followup_url,
                title="Followup Auto Source",
                text="Followup source verified fact.",
            ))
            tools.knowledge_write({
                "type": "fact",
                "title": "Followup Auto Fact",
                "body": "Followup source verified fact.",
                "sources": [followup_url],
                "evidence": [{
                    "source_url": followup_url,
                    "excerpt": "Followup source verified fact.",
                    "claim": "Followup source verified fact.",
                }],
            })
            material = PlanExecutionResult(fresh_source_urls=(followup_url,))

            merged = merge_evidence_patch(initial_result, tools, material)

            assert "Initial source verified fact" in merged.summary
            assert "Followup source verified fact" in merged.summary
            assert "基于已验证来源的研究结论" not in merged.summary
            assert "未找到强反证" not in merged.summary
        finally:
            store.index.close()
