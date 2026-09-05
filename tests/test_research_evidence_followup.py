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
            assert "evidence to be a non-empty list" in res_no_ev

            # 6. Scalar sources are rejected; sources must be a URL list
            res_scalar_sources = controller.execute_tool_call("knowledge_write", {
                "type": "fact",
                "title": "Scalar Source Claim",
                "body": "Body",
                "sources": allowed_url,
                "evidence": [{
                    "source_url": allowed_url,
                    "excerpt": "Fresh source body",
                }],
            })
            assert "sources to be a non-empty list of URLs" in res_scalar_sources

            # 7. Singleton dict evidence is rejected; evidence must be a list
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
            assert "evidence to be a non-empty list" in res_bad_single

            # 8. Evidence source alias is rejected; source_url must be explicit
            res_source_alias = controller.execute_tool_call("knowledge_write", {
                "type": "fact",
                "title": "Alias Source Claim",
                "body": "Body",
                "sources": [allowed_url],
                "evidence": [{
                    "source": allowed_url,
                    "excerpt": "Fresh source body",
                }],
            })
            assert "'source' alias is not accepted" in res_source_alias

            # 9. Non-fact note type (e.g. concept) is rejected
            res_bad_type = controller.execute_tool_call("knowledge_write", {
                "type": "concept",
                "title": "Concept Note",
                "body": "Body",
                "sources": [allowed_url],
                "evidence": [{
                    "source_url": allowed_url,
                    "excerpt": "Fresh source body",
                }],
            })
            assert "requires type='fact', got 'concept'" in res_bad_type

            # 10. Missing explicit type field is rejected
            res_missing_type = controller.execute_tool_call("knowledge_write", {
                "title": "Missing Type Note",
                "body": "Body",
                "sources": [allowed_url],
                "evidence": [{
                    "source_url": allowed_url,
                    "excerpt": "Fresh source body",
                }],
            })
            assert "requires explicit type='fact'" in res_missing_type

            # 11. Evidence source_url not in note's sources is rejected for provenance integrity
            tools.sources_read.add("https://example.com/fresh2")
            controller_two = EvidenceFollowupController(tools, [allowed_url, "https://example.com/fresh2"])
            res_mismatch_src = controller_two.execute_tool_call("knowledge_write", {
                "type": "fact",
                "title": "Mismatch Sources Note",
                "body": "Body",
                "sources": [allowed_url],
                "evidence": [{
                    "source_url": "https://example.com/fresh2",
                    "excerpt": "Fresh source body",
                }],
            })
            assert "must be declared in the note's 'sources' list" in res_mismatch_src

            # 12. Evidence-only mode rejects ordinary knowledge_write side channels
            res_extra_args = controller.execute_tool_call("knowledge_write", {
                "type": "fact",
                "title": "Tagged Claim",
                "body": "Body",
                "sources": [allowed_url],
                "evidence": [{
                    "source_url": allowed_url,
                    "excerpt": "Fresh source body",
                }],
                "tags": ["research"],
                "relations": [],
            })
            assert "accepts only type/title/body/sources/evidence args" in res_extra_args
            assert "relations" in res_extra_args
            assert "tags" in res_extra_args
            assert len(tools.ledger.evidence_items) == 1
        finally:
            store.index.close()


def test_run_evidence_followup_rejects_multiple_tool_calls() -> None:
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
            # Model attempts to output multiple tool calls
            reply = """```json
{"tool": "knowledge_write", "args": {"type": "fact", "title": "Fact 1", "body": "Body 1", "sources": ["https://example.com/fresh"], "evidence": [{"source_url": "https://example.com/fresh", "excerpt": "Excerpt 1"}]}}
```
```json
{"tool": "knowledge_write", "args": {"type": "fact", "title": "Fact 2", "body": "Body 2", "sources": ["https://example.com/fresh"], "evidence": [{"source_url": "https://example.com/fresh", "excerpt": "Excerpt 2"}]}}
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
            assert result.stop_reason == "invalid_tool_calls_count"
            assert "strictly requires exactly 1 tool call, got 2" in result.errors[0]
        finally:
            store.index.close()


def test_run_evidence_followup_rejects_missing_tool_field() -> None:
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
            # Model returns JSON without explicit "tool" field (or with "name" instead)
            reply = """```json
{"name": "knowledge_write", "args": {"type": "fact", "title": "Fact 1", "body": "Body 1", "sources": ["https://example.com/fresh"], "evidence": [{"source_url": "https://example.com/fresh", "excerpt": "Excerpt 1"}]}}
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
            assert result.stop_reason == "missing_tool_name"
            assert "requires explicit 'tool': 'knowledge_write'" in result.errors[0]
        finally:
            store.index.close()


def test_run_evidence_followup_rejects_missing_args_field() -> None:
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
            # Model returns JSON with tool but flat fields instead of "args" dict
            reply = """```json
{"tool": "knowledge_write", "type": "fact", "title": "Fact 1", "body": "Body 1", "sources": ["https://example.com/fresh"], "evidence": [{"source_url": "https://example.com/fresh", "excerpt": "Excerpt 1"}]}
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
            assert result.stop_reason == "missing_tool_args"
            assert "requires explicit 'args': {...} dictionary" in result.errors[0]
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
{"tool": "web_search", "args": {"query": "forbidden search"}}
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


def test_run_evidence_followup_classifies_no_relevant_done_as_noop() -> None:
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
            reply = """```json
{"tool": "done", "args": {"answer": "The fresh URLs are unrelated to the research question, so there is no relevant evidence to extract."}}
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
            assert result.has_new_evidence is False
            assert result.stop_reason == "no_relevant_material"
            assert result.new_source_urls == ("https://example.com/fresh",)
            assert "no relevant evidence" in result.errors[0]
        finally:
            store.index.close()


def test_run_evidence_followup_classifies_done_without_evidence_as_noop() -> None:
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
            reply = """```json
{"tool": "done", "args": {"answer": "Follow-up complete."}}
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
            assert result.has_new_evidence is False
            assert result.stop_reason == "no_evidence_extracted"
            assert result.new_source_urls == ("https://example.com/fresh",)
            assert "without writing evidence" in result.errors[0]
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
{"tool": "knowledge_write", "args": {"type": "fact", "title": "Fresh Fact", "body": "Fact description", "sources": ["https://example.com/fresh"], "evidence": [{"source_url": "https://example.com/fresh", "excerpt": "Fresh source body", "claim": "Fresh Fact"}]}}
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
