"""Research lifecycle pipeline for bounded planner execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from codey.runtime import cancellation
from codey.research.context import ResearchContext, ResearchPipelineConfig
from codey.research.evidence_followup import EvidenceFollowupResult
from codey.research.evidence_ledger import EvidenceLedgerStore, EvidenceLedgerWriteResult
from codey.research.evidence_runtime import snapshot_from_research_record
import codey.research.followup_selection as followup_selection
from codey.research.brief_projection import project_research_brief
from codey.research.plan_executor import PlanExecutionResult, PlanExecutor
from codey.research.proof_quality import ResearchProofReview, review_research_proof
from codey.research.query_planner import ResearchPlan, build_research_plan
from codey.research.record_merge import merge_evidence_patch
from codey.research.review_finding import findings_from_proof_review, planner_gaps_from_findings
from codey.research.runner import ResearchRunResult
from codey.research.source_trust import project_source_set
from codey.research.tools import ResearchTools


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
        tools: ResearchTools | None = None,
        iteration_context: str = "",
        topic_continuity_context: str = "",
        topic_continuity_payload: Mapping[str, object] | None = None,
    ) -> ResearchIterationRun:
        ...


class EvidenceFollowupRunner(Protocol):
    def __call__(
        self,
        *,
        tools: ResearchTools,
        plan: ResearchPlan,
        material: PlanExecutionResult,
        question: str,
        initial_summary: str = "",
        max_context_chars: int = 8000,
        should_stop: Callable[[], bool] | None = None,
    ) -> EvidenceFollowupResult:
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
            "followup_applied": bool(self.followup_applied),
            "followup_rounds": max(0, min(3, int(self.followup_rounds or 0))),
            "stop_reason": str(self.stop_reason or ""),
            "planner_stop_reason": str(self.planner_stop_reason or ""),
            "fresh_source_count": max(0, int(self.fresh_source_count or 0)),
            "new_evidence_count": max(0, int(self.new_evidence_count or 0)),
            "final_evidence_count": max(0, int(self.final_evidence_count or 0)),
            "attempted_fresh_source_count": max(0, int(self.attempted_fresh_source_count or 0)),
            "attempted_new_evidence_count": max(0, int(self.attempted_new_evidence_count or 0)),
        }


class ResearchPipeline:

    def __init__(
        self,
        *,
        context: ResearchContext,
        run_iteration: ResearchIterationRunner,
        search_factory: Callable[[], object],
        evidence_followup_runner: EvidenceFollowupRunner | None = None,
        evidence_ledgers: EvidenceLedgerStore | None = None,
        config: ResearchPipelineConfig | None = None,
        ledger_event_sink: Callable[[EvidenceLedgerWriteResult], None] | None = None,
        research_changes_sink: Callable[[str, object], None] | None = None,
    ) -> None:
        self.context = context
        self.run_iteration = run_iteration
        self.search_factory = search_factory
        self.evidence_followup_runner = evidence_followup_runner
        self.evidence_ledgers = evidence_ledgers
        self.config = config or ResearchPipelineConfig()
        self.ledger_event_sink = ledger_event_sink
        self.research_changes_sink = research_changes_sink

    def run(self) -> ResearchPipelineResult:
        search = self.search_factory()
        try:
            # Topic continuity travels to the initial iteration as bounded
            # text plus its digest-only payload; the runner projects the
            # audit row at the provider-send boundary, so an admitted row is
            # always bound to outbound provider-send attempt bytes.
            initial_run = self.run_iteration(
                task=self.context.question,
                max_turns=self.context.max_turns,
                chat_handoff=self.context.chat_handoff,
                search=search,
                topic_continuity_context=self.context.topic_continuity_context,
                topic_continuity_payload=self.context.topic_continuity_payload,
            )
            initial = initial_run.result
            best = initial
            best_tools = initial_run.tools
            best_review = self._review(best, require_ledger_record=False)
            plan = self._plan(best_review)
            self.context.trace.record_plan(plan)
            followup_rounds = 0
            total_fresh_sources = 0
            total_new_evidence = 0
            total_attempted_fresh_sources = 0
            total_attempted_new_evidence = 0
            total_merged_evidence = len(getattr(best.research_record, "evidence", ())) if getattr(best, "research_record", None) else 0
            planner_stop_reason = self._followup_block_reason(initial, best_review, plan) or "planned"
            if planner_stop_reason == "planned":
                if best_tools is None:
                    planner_stop_reason = "missing_iteration_tools"
                elif self.evidence_followup_runner is None:
                    planner_stop_reason = "missing_evidence_followup_runner"
                else:
                    current_tools = best_tools
                    max_rounds = self._max_rounds()
                    for round_index in range(1, max_rounds + 1):
                        if self.context.should_stop():
                            planner_stop_reason = "stopped"
                            break
                        staged_tools = current_tools.create_staged()

                        executor = PlanExecutor(
                            config=self.config,
                            should_stop=self.context.should_stop,
                        )
                        try:
                            material = executor.execute(plan, staged_tools)
                        except cancellation.TaskCancelled:
                            raise
                        except Exception:
                            planner_stop_reason = "followup_execution_error"
                            break
                        planner_stop_reason = material.stop_reason
                        attempted_sources = len(material.fresh_source_urls) if material.fresh_source_urls else max(0, int(material.fresh_source_count or 0))
                        total_attempted_fresh_sources += attempted_sources
                        if not material.has_new_material:
                            break
                        try:
                            followup_result = self.evidence_followup_runner(
                                tools=staged_tools,
                                plan=plan,
                                material=material,
                                question=self.context.question,
                                initial_summary=best.summary,
                                max_context_chars=self.config.max_followup_context_chars,
                                should_stop=self.context.should_stop,
                            )
                        except cancellation.TaskCancelled:
                            raise
                        except Exception:
                            planner_stop_reason = "followup_iteration_error"
                            break
                        attempted_ev = max(0, int(followup_result.new_evidence_count or 0))
                        total_attempted_new_evidence += attempted_ev
                        if not followup_result.has_new_evidence:
                            planner_stop_reason = followup_result.stop_reason or "no_evidence_extracted"
                            break
                        candidate = merge_evidence_patch(
                            initial=best,
                            tools=staged_tools,
                            material=material,
                        )
                        candidate_review = self._review(candidate, require_ledger_record=False)
                        if followup_selection.selects_candidate(candidate, candidate_review, best, best_review):
                            try:
                                best_tools.commit_staged(staged_tools)
                            except cancellation.TaskCancelled:
                                raise
                            except Exception:
                                planner_stop_reason = "followup_commit_error"
                                break
                            best = candidate
                            best_review = candidate_review
                            current_tools = best_tools
                            followup_rounds = round_index
                            total_fresh_sources += attempted_sources
                            total_new_evidence += attempted_ev
                            total_merged_evidence = len(getattr(best.research_record, "evidence", ())) if getattr(best, "research_record", None) else 0
                            planner_stop_reason = "evidence_merged"
                        else:
                            planner_stop_reason = "candidate_not_selected"
                            break
                        plan = self._plan(best_review)
                        self.context.trace.record_plan(plan)
                        block_reason = self._followup_block_reason(best, best_review, plan)
                        if block_reason:
                            planner_stop_reason = block_reason
                            break
                    else:
                        if (
                            followup_rounds >= max_rounds
                            and self._followup_block_reason(best, best_review, plan) == ""
                        ):
                            planner_stop_reason = "max_followup_rounds"

            ledger_result = self._append_final_record(best)
            self.context.trace.record_evidence_ledger_write(ledger_result)
            if self.ledger_event_sink is not None and ledger_result is not None:
                self.ledger_event_sink(ledger_result)
            self.context.trace.record_result(best)
            final_review = self._review(best, require_ledger_record=self.evidence_ledgers is not None)
            self.context.trace.record_proof_review(final_review)
            self._record_final_findings(best, final_review)
            self.context.trace.record_plan(self._plan(final_review))
            self._record_research_changes(best_tools)
            output = ResearchPipelineResult(
                final_result=best,
                followup_applied=followup_rounds > 0,
                followup_rounds=followup_rounds,
                stop_reason=best.stop_reason,
                planner_stop_reason=planner_stop_reason,
                fresh_source_count=total_fresh_sources,
                new_evidence_count=total_new_evidence,
                final_evidence_count=total_merged_evidence,
                attempted_fresh_source_count=total_attempted_fresh_sources,
                attempted_new_evidence_count=total_attempted_new_evidence,
            )

            self.context.trace.record_pipeline_result(output)
            return output

        finally:
            close = getattr(search, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def _followup_block_reason(
        self,
        result: ResearchRunResult,
        review: ResearchProofReview | None,
        plan: ResearchPlan,
    ) -> str:
        if not self.config.enabled:
            return "disabled"
        if self._max_rounds() <= 0:
            return "max_followup_rounds"
        if review is None:
            return "proof_review_missing"
        if not followup_selection.has_actionable_gap(review):
            return followup_selection.pipeline_stop_reason(plan, review)
        if not plan.query_candidates:
            return followup_selection.pipeline_stop_reason(plan, review)
        if not followup_selection.is_followup_eligible_stop(result.stop_reason):
            reason = str(result.stop_reason or "unknown").strip() or "unknown"
            return "initial_stop_reason_" + _reason_code(reason)
        if self.context.should_stop():
            return "stopped"
        return ""

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
            max_queries=self.config.max_queries_per_round,
            max_sources=self.config.max_total_sources,
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
        if self.evidence_ledgers is None:
            return None
        record = getattr(result, "research_record", None)
        if record is None:
            return None
        try:
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
        self.research_changes_sink(self.context.run_id, tools.changes)

    def _max_rounds(self) -> int:
        try:
            return max(0, min(3, int(self.config.max_followup_rounds or 0)))
        except (TypeError, ValueError):
            return 0

def _reason_code(value: object) -> str:
    text = str(value or "").strip()
    rendered = "".join(char if char.isalnum() or char in "._:-" else "_" for char in text)
    return rendered[:80] or "unknown"


__all__ = [
    "EvidenceFollowupRunner",
    "ResearchIterationRun",
    "ResearchIterationRunner",
    "ResearchPipeline",
    "ResearchPipelineResult",
]
