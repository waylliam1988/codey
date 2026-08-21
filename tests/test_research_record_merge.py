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

            # 4. Merging again with same inputs is idempotent
            merged_again = merge_evidence_patch(merged_result, tools, material)
            assert merged_again.summary == merged_result.summary
            assert merged_again.research_record.record_digest == merged_result.research_record.record_digest
        finally:
            store.index.close()
