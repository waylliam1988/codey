"""Run one research writer and project its settled audit and knowledge changes."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from codey.research.brief_projection import project_research_brief
from codey.research.context import ResearchContext
from codey.research.evidence_ledger import EvidenceLedgerStore, EvidenceLedgerWriteResult
from codey.research.evidence_runtime import snapshot_from_research_record
from codey.research.proof_quality import ResearchProofReview, review_research_proof
from codey.research.query_planner import ResearchPlan, build_research_plan
from codey.research.review_finding import findings_from_proof_review, planner_gaps_from_findings
from codey.research.run_result import ResearchRunResult
from codey.research.source_trust import project_source_set
from codey.research.tools import ResearchTools


class ResearchContinuationError(RuntimeError):
    """A committed research continuation cannot be safely reconstructed."""


@dataclass(frozen=True)
class ResearchIterationRun:
    result: ResearchRunResult
    tools: ResearchTools | None = None


class ResearchIterationRunner(Protocol):
    def __call__(
        self,
        *,
        task: str,
        max_turns: int,
        chat_handoff: str,
        search: object,
        topic_continuity_context: str = "",
        topic_continuity_payload: Mapping[str, object] | None = None,
    ) -> ResearchIterationRun:
        ...


@dataclass(frozen=True)
class ResearchPipelineResult:
    final_result: ResearchRunResult
    followup_applied: bool = False
    followup_rounds: int = 0
    stop_reason: str = ""
    planner_stop_reason: str = ""
    fresh_source_count: int = 0
    new_evidence_count: int = 0
    final_evidence_count: int = 0
    attempted_fresh_source_count: int = 0
    attempted_new_evidence_count: int = 0

    def to_payload(self) -> dict[str, object]:
        return {
            "followup_applied": self.followup_applied,
            "followup_rounds": max(0, min(3, (self.followup_rounds or 0))),
            "stop_reason": (self.stop_reason or ""),
            "planner_stop_reason": (self.planner_stop_reason or ""),
            "fresh_source_count": max(0, (self.fresh_source_count or 0)),
            "new_evidence_count": max(0, (self.new_evidence_count or 0)),
            "final_evidence_count": max(0, (self.final_evidence_count or 0)),
            "attempted_fresh_source_count": max(0, (self.attempted_fresh_source_count or 0)),
            "attempted_new_evidence_count": max(0, (self.attempted_new_evidence_count or 0)),
        }


class ResearchPipeline:
    def __init__(
        self,
        *,
        context: ResearchContext,
        run_iteration: ResearchIterationRunner,
        search_factory: Callable[[], object],
        evidence_ledgers: EvidenceLedgerStore | None = None,
        ledger_event_sink: Callable[[EvidenceLedgerWriteResult], None] | None = None,
        research_changes_sink: Callable[..., None] | None = None,
        policy: object,
    ) -> None:
        self.context = context
        self.run_iteration = run_iteration
        self.search_factory = search_factory
        self.evidence_ledgers = evidence_ledgers
        self.ledger_event_sink = ledger_event_sink
        self.research_changes_sink = research_changes_sink
        self.policy = policy

    def run(self) -> ResearchPipelineResult:
        search = self.search_factory()
        try:
            run = self.run_iteration(
                task=self.context.question,
                max_turns=self.context.max_turns,
                chat_handoff=self.context.chat_handoff,
                search=search,
                topic_continuity_context=self.context.topic_continuity_context,
                topic_continuity_payload=self.context.topic_continuity_payload,
            )
            review = self._review(run.result, require_ledger_record=False)
            self.context.trace.record_plan(self._plan(review))
            return self._finalize_pipeline_result(run.result, run.tools)
        finally:
            close = getattr(search, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()

    def _finalize_pipeline_result(
        self, result: ResearchRunResult, tools: ResearchTools | None,
    ) -> ResearchPipelineResult:
        ledger_result = self._append_final_record(result)
        self.context.trace.record_evidence_ledger_write(ledger_result)
        if self.ledger_event_sink is not None and ledger_result is not None:
            self.ledger_event_sink(ledger_result)
        self.context.trace.record_result(result)
        review = self._review(result, require_ledger_record=self.evidence_ledgers is not None and result.accepted_report is None)
        self.context.trace.record_proof_review(review)
        self._record_final_findings(result, review)
        self.context.trace.record_plan(self._plan(review))
        self._record_research_changes(tools)
        # Preserve the UI/trace payload shape. Acquisition belongs to the writer;
        # these historical outer-followup counters are zero for new research.
        output = ResearchPipelineResult(
            final_result=result,
            stop_reason=result.stop_reason,
            planner_stop_reason=("strict_writer_owns_followup" if getattr(self.policy, "strict_research", False)
                                 else "external_followup_retired"),
            final_evidence_count=len(getattr(result.research_record, "evidence", ())) if result.research_record else 0,
        )
        self.context.trace.record_pipeline_result(output)
        return output

    def _review(
        self,
        result: ResearchRunResult,
        *,
        require_ledger_record: bool,
    ) -> ResearchProofReview | None:
        record = getattr(result, "research_record", None)
        if record is None:
            return None
        ledger_payload = self._ledger_payload() if require_ledger_record else None
        try:
            return review_research_proof(
                record,
                question=(
                    self.context.effective_proof_question
                    or str(getattr(result, "question", "") or "").strip()
                    or self.context.question
                ),
                evidence_ledger=ledger_payload,
                require_ledger_record=require_ledger_record,
            )
        except Exception:
            return None

    def _plan(self, review: ResearchProofReview | None) -> ResearchPlan:
        return build_research_plan(
            review,
            question=self.context.effective_proof_question or self.context.question,
            max_queries=3,
            max_sources=6,
        )

    def _record_final_findings(
        self,
        result: ResearchRunResult,
        review: ResearchProofReview | None,
    ) -> None:
        """Audit-only projections: trace sink, never the planner.

        Findings, planner gaps, source-trust rows, and the brief projection
        are deterministic read models recorded for later inspection. Any
        consumer that lets them influence search or prompts is a behavior
        change and needs its own A/B per the roadmap.
        """

        if review is None:
            return
        record = getattr(result, "research_record", None)
        try:
            snapshot = snapshot_from_research_record(record, proof_review=review)
            if snapshot is None:
                return
            findings = findings_from_proof_review(review, snapshot)
            if findings:
                self.context.trace.record_review_findings(findings)
                gaps = planner_gaps_from_findings(findings)
                if gaps:
                    self.context.trace.record_planner_gaps(gaps)
            self.context.trace.record_research_source_trust(
                project_source_set(getattr(record, "sources", ()) or ())
            )
            brief = project_research_brief(record, snapshot=snapshot, findings=findings)
            if brief is not None:
                self.context.trace.record_research_brief_projection(brief.to_payload())
        except Exception:
            return

    def _append_final_record(self, result: ResearchRunResult) -> EvidenceLedgerWriteResult | None:
        if result.accepted_report is not None and result.stop_reason != "done":
            return None
        if self.evidence_ledgers is None:
            return None
        record = getattr(result, "research_record", None)
        if record is None:
            return None
        try:
            if result.accepted_report is not None:
                return self.evidence_ledgers.append_accepted_report(
                    result.accepted_report.to_payload(), question=self.context.question,
                    run_id=self.context.run_id, session_id=self.context.session_id,
                    project=self.context.project, synthesis_id=result.synthesis_id)
            return self.evidence_ledgers.append_record(
                record,
                run_id=self.context.run_id,
                session_id=self.context.session_id,
                project=self.context.project,
            )
        except Exception:
            return EvidenceLedgerWriteResult(
                skipped=True,
                reason_code="write_failed",
                record_id=getattr(record, "record_id", ""),
            )

    def _ledger_payload(self) -> Mapping[str, object] | None:
        if self.evidence_ledgers is None:
            return None
        try:
            snapshot = self.evidence_ledgers.load(
                session_id=self.context.session_id,
                project=self.context.project,
            )
            if getattr(snapshot, "available", False):
                return snapshot.payload
        except Exception:
            return None
        return None

    def _record_research_changes(self, tools: ResearchTools | None) -> None:
        if self.research_changes_sink is None or not self.context.run_id:
            return
        if tools is None:
            return
        self.research_changes_sink(
            self.context.run_id, tools.changes, self.context.session_id
        )


__all__ = [
    "ResearchIterationRun",
    "ResearchIterationRunner",
    "ResearchPipeline",
    "ResearchPipelineResult",
]
