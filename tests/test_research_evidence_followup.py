from __future__ import annotations

import tempfile
from pathlib import Path

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.research.evidence_followup import (
    EvidenceFollowupController,
    build_evidence_followup_prompt,
    run_evidence_followup,
)
from codey.research.plan_executor import PlanExecutionResult
from codey.research.query_planner import QueryCandidate, ResearchPlan
from codey.research.tools import ResearchTools


class _MockProvider:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.sent_prompts: list[str] = []

    def send(self, prompt: str) -> str:
        self.sent_prompts.append(prompt)
        return self.reply


class _DummySearch:
    def close(self) -> None:
        pass


def test_evidence_followup_controller_restricts_tools_and_urls() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        try:
            tools = ResearchTools(
                search=_DummySearch(),
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-ev",
                project="project-ev",
            )
            # Register opened source in ledger so knowledge_write doesn't reject as un-opened
            from codey.research.source_document import SourceDocument
            tools.sources_read.add("https://example.com/fresh")
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url="https://example.com/fresh",
                final_url="https://example.com/fresh",
                title="Fresh Source",
                text="Fresh source body containing verified facts",
            ))

            allowed_url = "https://example.com/fresh"
            controller = EvidenceFollowupController(tools, [allowed_url])

            # 1. Non-knowledge_write tool is blocked
            res = controller.execute_tool_call("web_search", {"query": "test"})
            assert res.startswith("ERROR: Tool 'web_search' is forbidden")

            res_done = controller.execute_tool_call("done", {})
            assert res_done.startswith("ERROR: Tool 'done' is forbidden")

            # 2. s1/s2 internal source ID is blocked
            res_s1 = controller.execute_tool_call("knowledge_write", {
                "type": "fact",
                "title": "Claim Title",
                "body": "Claim Body",
                "sources": ["s1"],
            })
            assert "Internal IDs like s1/s2 are strictly forbidden" in res_s1

            # 3. URL not in whitelist is blocked
            res_unauth = controller.execute_tool_call("knowledge_write", {
                "type": "fact",
                "title": "Claim Title",
                "body": "Claim Body",
                "sources": ["https://example.com/other"],
            })
            assert "is not in the allowed fresh material whitelist" in res_unauth

            # 4. Valid whitelisted URL with evidence succeeds
            res_ok = controller.execute_tool_call("knowledge_write", {
                "type": "fact",
                "title": "Verified Fact",
                "body": "Detailed fact body",
                "sources": [allowed_url],
                "evidence": [{
                    "source_url": allowed_url,
                    "excerpt": "Fresh source body",
                    "claim": "Verified Fact",
                }],
            })
            assert res_ok.startswith("saved fact note id=")
            assert len(tools.ledger.evidence_items) == 1

            # 5. Missing explicit evidence is blocked in evidence-only mode
            res_no_ev = controller.execute_tool_call("knowledge_write", {
                "type": "fact",
                "title": "Implicit Claim",
                "body": "Body without evidence",
                "sources": [allowed_url],
            })
            assert "requires explicit 'evidence' items" in res_no_ev

            # 6. Singleton dict evidence with unauthorized source_url is blocked
            res_bad_single = controller.execute_tool_call("knowledge_write", {
                "type": "fact",
                "title": "Single Item Claim",
                "body": "Body",
                "sources": [allowed_url],
                "evidence": {
                    "source_url": "https://example.com/unauthorized",
                    "excerpt": "Fresh source body",
                },
            })
            assert "Evidence source_url 'https://example.com/unauthorized' is not in the allowed fresh material whitelist" in res_bad_single
        finally:
            store.index.close()


def test_run_evidence_followup_rejects_forbidden_tool_calls() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        try:
            tools = ResearchTools(
                search=_DummySearch(),
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-ev",
                project="project-ev",
            )
            # Model attempts to call done or search alongside knowledge_write
            reply = """```json
{"tool": "web_search", "query": "forbidden search"}
```"""
            provider = _MockProvider(reply)
            plan = ResearchPlan(plan_ref="plan:123")
            material = PlanExecutionResult(
                fresh_source_urls=("https://example.com/fresh",),
                previews=("Fresh source preview",),
            )

            result = run_evidence_followup(
                provider=provider,
                tools=tools,
                plan=plan,
                material=material,
                question="Question?",
            )
            assert result.ok is False
            assert result.stop_reason == "invalid_tool_called"
            assert "Forbidden tool 'web_search' was called" in result.errors[0]
        finally:
            store.index.close()



def test_run_evidence_followup_extracts_evidence_with_provider() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        try:
            tools = ResearchTools(
                search=_DummySearch(),
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-ev",
                project="project-ev",
            )
            from codey.research.source_document import SourceDocument
            tools.sources_read.add("https://example.com/fresh")
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url="https://example.com/fresh",
                final_url="https://example.com/fresh",
                title="Fresh Source",
                text="Fresh source body text excerpt",
            ))

            reply = """```json
{"tool": "knowledge_write", "type": "fact", "title": "Fresh Fact", "body": "Fact description", "sources": ["https://example.com/fresh"], "evidence": [{"source_url": "https://example.com/fresh", "excerpt": "Fresh source body", "claim": "Fresh Fact"}]}
```"""
            provider = _MockProvider(reply)
            plan = ResearchPlan(
                plan_ref="plan:123",
                query_candidates=(QueryCandidate("q:1", "fresh query"),),
            )
            material = PlanExecutionResult(
                fresh_source_urls=("https://example.com/fresh",),
                previews=("query: fresh query | Fresh Source | https://example.com/fresh\nFresh source body text excerpt",),
            )

            prompt = build_evidence_followup_prompt(
                question="What is the fresh fact?",
                initial_summary="Initial report summary",
                plan=plan,
                material=material,
            )
            assert "Target Research Question: What is the fresh fact?" in prompt
            assert "Allowed Fresh URLs:" in prompt
            assert "- https://example.com/fresh" in prompt

            result = run_evidence_followup(
                provider=provider,
                tools=tools,
                plan=plan,
                material=material,
                question="What is the fresh fact?",
            )

            assert result.ok is True
            assert result.has_new_evidence is True
            assert result.new_evidence_count == 1
            assert len(result.written_note_ids) == 1
            assert result.stop_reason == "written"
        finally:
            store.index.close()
