from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.research.context import ResearchContext, RunTraceResearchSink
from codey.research.evidence_ledger import EvidenceLedgerStore
from codey.research.ledger import ResearchLedger
from codey.research.object_model import build_research_record
from codey.research.pipeline import ResearchIterationRun, ResearchPipeline
from codey.research.proof_quality import CoverageGap, ResearchProofReview
from codey.research.query_planner import ResearchPlan
from codey.research.report_quality import review_report_quality
from codey.research.run_result import ResearchRunResult
from codey.research.tools import ResearchTools


def _allow_policy():
    # 管线 mechanics 测试：显式传入允许读写的父策略（生产恒有 entry policy）。
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({
        "control", "web.read", "knowledge.read", "knowledge.write", "knowledge.link",
    }))


class _TraceRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def call(*args, **kwargs) -> None:
            self.calls.append((name, args, kwargs))
        return call

    def record_permission_profile(self, *args, **kwargs) -> None:
        self.calls.append(("record_permission_profile", args, kwargs))

    def record_research_notes(self, *args, **kwargs) -> None:
        self.calls.append(("record_research_notes", args, kwargs))

    def record_research_sources(self, *args, **kwargs) -> None:
        self.calls.append(("record_research_sources", args, kwargs))

    def record_research_record_summary(self, *args, **kwargs) -> None:
        self.calls.append(("record_research_record_summary", args, kwargs))

    def record_research_plan(self, *args, **kwargs) -> None:
        self.calls.append(("record_research_plan", args, kwargs))

    def record_research_pipeline_result(self, *args, **kwargs) -> None:
        self.calls.append(("record_research_pipeline_result", args, kwargs))

    def record_research_proof_review(self, *args, **kwargs) -> None:
        self.calls.append(("record_research_proof_review", args, kwargs))

    def record_evidence_ledger_write(self, *args, **kwargs) -> None:
        self.calls.append(("record_evidence_ledger_write", args, kwargs))

    def flush(self) -> None:
        self.calls.append(("flush", (), {}))


class _PipelineSearch:
    def __init__(self) -> None:
        self.closed = False

    def search(self, *_args, **_kwargs) -> list[dict]:
        return []

    def fetch(self, url: str) -> dict:
        return {"status": "ok",
            "url": url,
            "title": "Pipeline source",
            "text": "Pipeline source text says the fact.",
            "truncated": False,
        }

    def close(self) -> None:
        self.closed = True


def _link_rows(store: KnowledgeStore, note_ids: list[str]) -> list[tuple[str, str, str]]:
    rows = store.index.links_touching(note_ids)
    return sorted(
        (
            str(row.get("src_id") or ""),
            str(row.get("dst_id") or ""),
            str(row.get("kind") or ""),
        )
        for row in rows
    )


def _record(
    *,
    question: str,
    synthesis_id: str,
    project: Path,
    url: str = "https://example.com/pipeline",
) -> object:
    ledger = ResearchLedger()
    summary = (
        "## 结论\n"
        "- Pipeline answer depends on the opened source. [1]\n\n"
        "## 关键证据\n"
        "- [1] Pipeline source text says the fact.\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；需要持续追踪新数据。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · fresh · example.com\n\n"
        "## 搜索覆盖\n"
        "- query: pipeline question\n"
        "- opened: Pipeline source\n"
        "- skipped: none representative\n\n"
        "## 来源\n"
        f"[1] Pipeline source - {url}"
    )
    ledger.record_search("pipeline question", [{
        "title": "Pipeline source",
        "url": url,
        "snippet": "Pipeline source text.",
    }])
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="Pipeline source",
        text="Pipeline source text says the fact.",
    )
    prepared = ledger.prepare_evidence_items(
        [{
            "claim": "Pipeline answer depends on the opened source.",
            "source_url": url,
            "excerpt": "Pipeline source text says the fact.",
            "stance": "supports",
        }],
        fallback_sources=[url],
        fallback_claim="Pipeline answer depends on the opened source.",
        fallback_body="Pipeline source text says the fact.",
        note_type="fact",
    )
    assert not prepared.error
    ledger.add_evidence_items(list(prepared.items), note_id="note-1")
    review = review_report_quality(
        summary,
        ledger=ledger,
        opened_sources=ledger.final_url_set(),
        search_result_urls={url},
    )
    assert review.ok
    return build_research_record(
        question=question,
        summary=summary,
        ledger=ledger,
        review=review,
        run_id="run-" + synthesis_id,
        session_id="session-pipeline",
        project=project,
        synthesis_id=synthesis_id,
        stop_reason="done",
    )


def _result(
    *,
    question: str,
    summary: str,
    stop_reason: str,
    synthesis_id: str,
    record,
    tools: ResearchTools,
    notes: list[str] | None = None,
) -> ResearchIterationRun:
    return ResearchIterationRun(
        result=ResearchRunResult(
            question=question,
            summary=summary,
            stop_reason=stop_reason,
            turns=1,
            notes_created=notes or [],
            opened_sources=[{
                "requested_url": "https://example.com/pipeline",
                "final_url": "https://example.com/pipeline",
                "title": "Pipeline source",
            }],
            synthesis_id=synthesis_id,
            research_record=record,
        ),
        tools=tools,
    )


def _review(
    *,
    record_id: str,
    record_digest: str,
    ok: bool,
    answer_status: str,
    score: float,
    missing: tuple[str, ...] = (),
) -> ResearchProofReview:
    return ResearchProofReview(
        ok=ok,
        answers_question=True,
        answer_status=answer_status,
        answer_coverage_score=score,
        citation_present=True,
        citation_locator_verified=True,
        support_relation_verified=True,
        counterevidence_checked=True,
        ledger_record_verified=False,
        question_digest="sha256:" + "1" * 64,
        coverage_gaps=(CoverageGap(reason_code="coverage_gap"),) if not ok else (),
        followup_questions=(),
        query_rewrite_candidates=(),
        source_trust_warnings=(),
        overclaim_warnings=(),
        stale_warnings=(),
        missing_evidence=missing,
        proof_ref="research_proof:" + answer_status,
        record_id=record_id,
        record_digest=record_digest,
    )


def test_staged_knowledge_store_read_through() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        parent_store = KnowledgeStore(root / "knowledge")
        from codey.knowledge.note import KnowledgeNote
        from codey.research.staging import StagedKnowledgeStore

        # 1. Note in parent
        parent_note = KnowledgeNote(
            id="parent-note",
            title="Parent Title",
            body="Parent Body",
            type="fact",
        )
        parent_store.write_note(parent_note)

        staged_store = StagedKnowledgeStore(parent_store)

        # Read through parent note
        assert staged_store.exists("parent-note") is True
        read_parent = staged_store.read_note("parent-note")
        assert read_parent is not None
        assert read_parent.title == "Parent Title"

        # 2. Note in staging only
        staged_note = KnowledgeNote(
            id="staged-note",
            title="Staged Title",
            body="Staged Body",
            type="fact",
        )
        staged_store.write_note(staged_note)

        assert staged_store.exists("staged-note") is True
        assert parent_store.exists("staged-note") is False
        read_staged = staged_store.read_note("staged-note")
        assert read_staged is not None
        assert read_staged.title == "Staged Title"


def test_knowledge_write_update_merges_existing_provenance_and_scope() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        changes = KnowledgeChanges(store.root)
        tools = ResearchTools(
            search=_PipelineSearch(),
            store=store,
            changes=changes,
            session_id="session-a",
            project="project-a",
        )
        source = "https://example.com/source"
        tools.sources_read.add(source)
        tools.ledger.record_open(source, source, "Source", "Original observed result. Updated observed result.")
        created = tools.knowledge_write({
            "type": "fact",
            "title": "Original fact",
            "body": "Original body.",
            "tags": ["original"],
            "sources": [source],
            "evidence": [{"source_url": source, "excerpt": "Original observed result."}],
            "relations": [{"src": "alpha", "dst": "beta", "kind": "affects"}],
            "aliases": ["Alpha"],
            "confidence": 0.7,
        }).model_text
        note_id = tools.created_ids[0]
        original = store.read_note(note_id)
        assert original is not None

        updated = tools.knowledge_write({
            "id": note_id,
            "title": "Updated fact",
            "body": "Updated body.",
            "evidence": [{"source_url": source, "excerpt": "Updated observed result."}],
        }).model_text
        note = store.read_note(note_id)
        assert note is not None

        assert created.startswith("saved fact note")
        assert updated.startswith("saved fact note")
        assert note.title == "Updated fact"
        assert note.body == "Updated body."
        assert note.type == "fact"
        assert note.sources == [source]
        assert note.relations == [{"src": "alpha", "dst": "beta", "kind": "affects"}]
        assert note.aliases == ["Alpha"]
        assert note.confidence == 0.7
        assert note.session_id == "session-a"
        assert note.project == "project-a"
        assert note.created == original.created


def test_knowledge_write_update_rejects_cross_session_owner() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        first = ResearchTools(
            search=_PipelineSearch(),
            store=store,
            changes=KnowledgeChanges(store.root),
            session_id="session-a",
            project="project-a",
        )
        source = "https://example.com/source"
        first.sources_read.add(source)
        first.ledger.record_open(source, source, "Source", "Original observed result.")
        first.knowledge_write({
            "type": "fact",
            "title": "Original fact",
            "body": "Original body.",
            "sources": [source],
            "evidence": [{"source_url": source, "excerpt": "Original observed result."}],
        })
        note_id = first.created_ids[0]
        second = ResearchTools(
            search=_PipelineSearch(),
            store=store,
            changes=KnowledgeChanges(store.root),
            session_id="session-b",
            project="project-a",
        )

        result = second.knowledge_write({
            "id": note_id,
            "title": "Rewrite",
            "body": "Wrong owner.",
        })

        assert result.ok is False
        assert result.model_text == "ERROR: note belongs to another session"
        note = store.read_note(note_id)
        assert note is not None
        assert note.title == "Original fact"


def test_staged_knowledge_store_rollback_on_partial_failure() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        target_store = KnowledgeStore(root / "knowledge")
        changes = KnowledgeChanges(root=target_store.root)
        from codey.knowledge.note import KnowledgeNote
        from codey.research.staging import StagedKnowledgeStore

        # Pre-populate an existing note in hypotheses folder
        existing_note = KnowledgeNote(id="existing-note", title="Original Title", body="Original Body", type="hypothesis")
        target_store.write_note(existing_note)
        orig_existing_path = target_store.path_for(existing_note)
        orig_existing_bytes = target_store.read_note(existing_note.id).to_markdown()

        staged_store = StagedKnowledgeStore(target_store)

        # 1. New note
        note1 = KnowledgeNote(id="note-1", title="Note 1", body="Body 1", type="fact")
        # 2. Existing note modified and moved from hypothesis to fact folder
        modified_existing = KnowledgeNote(id="existing-note", title="New Title", body="New Body", type="fact")
        # 3. Flaky note that fails
        note3 = KnowledgeNote(id="note-3", title="Note 3", body="Body 3", type="fact")

        staged_store.write_note(note1)
        staged_store.write_note(modified_existing)
        staged_store.write_note(note3)

        original_write = target_store.write_note
        call_count = [0]

        def flaky_write_note(note, *, changes=None):
            call_count[0] += 1
            if call_count[0] == 3:
                raise OSError("Disk full on third note")
            return original_write(note, changes=changes)

        target_store.write_note = flaky_write_note

        # Commit fails on the 3rd note and rolls back note-1 and modified_existing completely
        with pytest.raises(OSError, match="Disk full on third note"):
            staged_store.commit_to(target_store, changes=changes)

        # Verify new note-1 was completely unlinked
        note1_path = target_store.path_for(note1)
        assert note1_path.exists() is False

        # Verify moved path (facts/existing-note.md) was cleaned up, and original (hypotheses/existing-note.md) is 100% byte-exact
        moved_path = target_store.path_for(modified_existing)
        assert moved_path.exists() is False
        assert not orig_existing_path.exists()
        assert target_store.read_note(existing_note.id).to_markdown() == orig_existing_bytes
        assert len(target_store.documents.history(existing_note.id)) == 1

        # Verify SQLite index is 100% clean
        assert target_store.exists("note-1") is False
        assert target_store.exists("note-3") is False
        assert target_store.exists("existing-note") is True
        assert target_store.index.count() == 1
        idx_existing = target_store.index.get("existing-note")
        assert idx_existing["title"] == "Original Title"
        assert idx_existing["type"] == "hypothesis"

        # Verify KnowledgeChanges is 100% clean: zero false created/updated records
        assert changes.created == []
        assert changes.updated == []
        assert changes.has_changes() is False


def test_staged_knowledge_store_link_validation() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        from codey.knowledge.note import KnowledgeNote
        from codey.research.staging import StagedKnowledgeStore

        staged_store = StagedKnowledgeStore(store)
        staged_store.write_note(KnowledgeNote(id="existing-note", title="T", body="B", type="fact"))

        # Link fails on unknown source
        res1 = staged_store.link("unknown-note", "existing-note").model_text
        assert "ERROR: unknown source note" in res1

        # Link fails on unknown target
        res2 = staged_store.link("existing-note", "unknown-note").model_text
        assert "ERROR: unknown target note" in res2

        # Link succeeds when both exist
        res3 = staged_store.link("existing-note", "existing-note").model_text
        assert res3.startswith("linked:")

        # Staging resolves titles like the real KnowledgeStore, including staged-only notes.
        store.write_note(KnowledgeNote(id="parent-note", title="Parent Title", body="Parent Body", type="fact"))
        staged_store.write_note(KnowledgeNote(id="staged-note", title="Staged Title", body="Staged Body", type="fact"))
        res4 = staged_store.link("Staged Title", "Parent Title", "supports").model_text
        assert res4 == "linked: staged-note -> parent-note (supports)"


def test_staged_knowledge_store_rollback_restores_links_after_link_failure() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        changes = KnowledgeChanges(root=store.root)
        from codey.knowledge.note import KnowledgeNote
        from codey.research.staging import StagedKnowledgeStore

        source = KnowledgeNote(id="source-note", title="Source Title", body="Source body.", type="fact")
        old_target = KnowledgeNote(id="old-target", title="Old Target", body="Old body.", type="fact")
        new_target = KnowledgeNote(id="new-target", title="New Target", body="New body.", type="fact")
        store.write_note(source)
        store.write_note(old_target)
        store.write_note(new_target)
        assert store.link("Source Title", "Old Target", "supports").model_text.startswith("linked:")
        original_body = store.read_note("source-note").body
        original_links = _link_rows(store, ["source-note", "old-target", "new-target"])

        staged_store = StagedKnowledgeStore(store)
        assert staged_store.link("Source Title", "New Target", "supports").model_text == (
            "linked: source-note -> new-target (supports)"
        )
        original_link = store.link

        def fail_after_link(src, dst, kind="relates", *, changes=None):
            result = original_link(src, dst, kind, changes=changes)
            assert result.ok is True
            raise OSError("link failed after side effect")

        store.link = fail_after_link

        with pytest.raises(OSError, match="link failed after side effect"):
            staged_store.commit_to(store, changes=changes)

        restored = store.read_note("source-note")
        assert restored is not None
        assert restored.body == original_body
        assert _link_rows(store, ["source-note", "old-target", "new-target"]) == original_links
        assert changes.created == []
        assert changes.updated == []
        assert changes.has_changes() is False


def test_content_hash_bytes_consistency() -> None:
    from codey.knowledge.store import _content_hash, content_hash_bytes

    text = "Hello Codey Knowledge Vault\nLine 2"
    raw_bytes = text.encode("utf-8")
    assert _content_hash(text) == content_hash_bytes(raw_bytes)
    assert len(content_hash_bytes(raw_bytes)) == 16


@pytest.mark.parametrize("strict", [True, False])
def test_pipeline_projects_final_review_findings_and_gaps_to_trace_only(strict) -> None:
    """Actionable audit gaps never launch outer acquisition or another writer."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        changes = KnowledgeChanges(root=store.root)
        search = _PipelineSearch()
        tools = ResearchTools(
            search=search,
            store=store,
            changes=changes,
            session_id="session-findings",
            project="project-findings",
        )
        trace = _TraceRecorder()
        record = _record(question="Pipeline question", synthesis_id="initial", project=root / "project")
        conclusion = next(claim for claim in record.claims if claim.claim_section == "conclusion")
        broken = replace(
            record,
            claims=tuple(
                replace(conclusion, evidence_refs=(), status="unsupported", citation_numbers=())
                if claim.claim_id == conclusion.claim_id
                else claim
                for claim in record.claims
            ),
        )
        result = _result(
            question="Pipeline question",
            summary="initial summary",
            stop_reason="done",
            synthesis_id="initial",
            record=broken,
            tools=tools,
        )

        from types import SimpleNamespace

        from codey.operations.task_entry import build_task_policy_for_entry

        policy = build_task_policy_for_entry(SimpleNamespace(task="Pipeline question"), "research")
        if not strict:
            policy = replace(policy, strict_research=False)
        writer_calls = []

        def run_iteration(**kwargs):
            writer_calls.append(kwargs)
            return result

        def unexpected_acquisition(*_args, **_kwargs):
            raise AssertionError("Audit gaps must not trigger outer acquisition")

        search.search = search.fetch = unexpected_acquisition
        ledger = EvidenceLedgerStore(root / "state")
        ledger_events = []
        change_events = []
        context = ResearchContext(
            question="Pipeline question",
            session_id="session-findings",
            run_id="run-findings",
            project="project-findings",
            proof_question="Pipeline question",
            max_turns=4,
            trace=RunTraceResearchSink(trace),
        )

        pipeline = ResearchPipeline(
            context=context,
            run_iteration=run_iteration,
            search_factory=lambda: search,
            policy=policy,
            evidence_ledgers=ledger,
            ledger_event_sink=ledger_events.append,
            research_changes_sink=lambda *args: change_events.append(args),
        )
        output = pipeline.run()

        assert len(writer_calls) == 1
        assert writer_calls[0]["task"] == "Pipeline question"
        assert output.planner_stop_reason == ("strict_writer_owns_followup" if strict else "external_followup_retired")
        assert len(ledger_events) == 1 and not ledger_events[0].skipped
        assert ledger.load(session_id=context.session_id, project=context.project).available
        assert change_events == [(context.run_id, changes, context.session_id)]
        # Audit findings cannot launch another writer or source acquisition.
        assert output.followup_applied is False
        assert output.final_result is result.result
        assert search.closed is True

        names = [name for name, *_ in trace.calls]
        assert "record_research_proof_review" in names

        finding_calls = [call for call in trace.calls if call[0] == "record_review_findings"]
        assert len(finding_calls) == 1
        payloads = finding_calls[0][1][0]
        kinds = {payload["kind"] for payload in payloads}
        assert "unsupported_claim" in kinds
        unsupported = next(payload for payload in payloads if payload["kind"] == "unsupported_claim")
        assert unsupported["claim_ref"] == conclusion.claim_id
        assert unsupported["severity"] == "critical"
        assert all(payload["finding_id"].startswith("review_finding:") for payload in payloads)
        serialized = json.dumps(payloads, ensure_ascii=False)
        assert "claim_text" not in serialized
        assert conclusion.claim_text not in serialized

        gap_calls = [call for call in trace.calls if call[0] == "record_planner_gaps"]
        assert len(gap_calls) == 1
        gap_payloads = gap_calls[0][1][0]
        gap_kinds = {gap["gap_kind"] for gap in gap_payloads}
        assert {"followup_search", "locator_verification"} <= gap_kinds
        assert all(gap["gap_id"].startswith("planner_gap:") for gap in gap_payloads)

        # Findings are recorded after the proof review and before the final plan.
        idx_review = names.index("record_research_proof_review")
        idx_findings = names.index("record_review_findings")
        idx_plan_after = max(i for i, name in enumerate(names) if name == "record_research_plan")
        assert idx_review < idx_findings < idx_plan_after
        plans = [call[1][0] for call in trace.calls if call[0] == "record_research_plan"]
        assert any(plan["query_count"] > 0 for plan in plans)


def test_pipeline_surfaces_ledger_append_raise_as_write_failed() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        state_root = root / "state"
        store = KnowledgeStore(root / "knowledge")
        changes = KnowledgeChanges(root=store.root)
        search = _PipelineSearch()
        tools = ResearchTools(search=search, store=store, changes=changes, session_id="session-pipeline", project="project-pipeline")
        trace = _TraceRecorder()
        record = _record(question="Pipeline question", synthesis_id="initial", project=project)
        result = _result(
            question="Pipeline question",
            summary="initial summary",
            stop_reason="done",
            synthesis_id="initial",
            record=record,
            tools=tools,
        )
        evidence_ledgers = EvidenceLedgerStore(state_root)
        events: list[tuple[str, object]] = []

        def run_iteration(**_kwargs):
            return result

        def fake_review(record, *, question: str = "", evidence_ledger=None, require_ledger_record: bool = False):
            del question, evidence_ledger
            return replace(
                _review(
                    record_id=getattr(record, "record_id", ""),
                    record_digest=getattr(record, "record_digest", ""),
                    ok=True,
                    answer_status="answered",
                    score=1.0,
                ),
                ledger_record_verified=require_ledger_record,
            )

        def fake_plan(review, *, question: str = "", max_queries: int, max_sources: int):
            del question
            return ResearchPlan(
                plan_ref="research_plan:noop",
                proof_ref=getattr(review, "proof_ref", ""),
                query_candidates=(),
                reason_codes=("proof_ok_no_required_followup",),
                max_queries=max_queries,
                max_sources=max_sources,
            )

        context = ResearchContext(
            question="Pipeline question",
            session_id="session-pipeline",
            run_id="run-pipeline",
            project="project-pipeline",
            proof_question="Pipeline question",
            max_turns=4,
            trace=RunTraceResearchSink(trace),
        )

        from codey.research import pipeline as pipeline_module

        original_review = pipeline_module.review_research_proof
        original_plan = pipeline_module.build_research_plan
        try:
            pipeline_module.review_research_proof = fake_review
            pipeline_module.build_research_plan = fake_plan
            with mock.patch.object(
                EvidenceLedgerStore,
                "append_record",
                side_effect=OSError("disk"),
            ):
                pipeline = ResearchPipeline(
                    context=context,
                    run_iteration=run_iteration,
                    search_factory=lambda: search,
                    policy=_allow_policy(),
                    evidence_ledgers=evidence_ledgers,
                    ledger_event_sink=lambda item: events.append(("ledger", item)),
                    research_changes_sink=lambda run_id, snapshot, _session_id="": events.append((run_id, snapshot)),
                )
                output = pipeline.run()
        finally:
            pipeline_module.review_research_proof = original_review
            pipeline_module.build_research_plan = original_plan

        assert output.final_result is result.result
        ledger_calls = [call for call in trace.calls if call[0] == "record_evidence_ledger_write"]
        assert len(ledger_calls) == 1
        assert ledger_calls[0][1][0]["reason_code"] == "write_failed"
        sink_items = [item for name, item in events if name == "ledger"]
        assert len(sink_items) == 1
        assert getattr(sink_items[0], "reason_code", "") == "write_failed"
