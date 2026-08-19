"""Narrow Research pipeline context and trace projection helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol

from codey.prompt_envelope import FailOpenPromptTrace
from codey.research.evidence_ledger import EvidenceLedgerWriteResult
from codey.research.proof_quality import ResearchProofReview, proof_review_trace_payload
from codey.research.query_planner import ResearchPlan, research_plan_trace_payload


class ResearchTraceSink(Protocol):
    def record_result(self, result: object) -> None:
        ...

    def record_proof_review(self, review: ResearchProofReview | Mapping[str, object] | None) -> None:
        ...

    def record_plan(self, plan: ResearchPlan | Mapping[str, object] | None) -> None:
        ...

    def record_evidence_ledger_write(self, result: EvidenceLedgerWriteResult | object | None) -> None:
        ...


class NullResearchTraceSink:
    def record_result(self, result: object) -> None:
        del result

    def record_proof_review(self, review: ResearchProofReview | Mapping[str, object] | None) -> None:
        del review

    def record_plan(self, plan: ResearchPlan | Mapping[str, object] | None) -> None:
        del plan

    def record_evidence_ledger_write(self, result: EvidenceLedgerWriteResult | object | None) -> None:
        del result


class RunTraceResearchSink:
    """Bounded Research projection over a RunTraceRecorder-like object."""

    def __init__(self, trace: object | None) -> None:
        self._sink = FailOpenPromptTrace(trace)

    def record_result(self, result: object) -> None:
        self._sink.call("record_permission_profile", "research", phase="research")
        self._sink.call(
            "record_research_notes",
            [
                *getattr(result, "notes_created", ()),
                *getattr(result, "notes_updated", ()),
                getattr(result, "synthesis_id", ""),
            ],
        )
        self._sink.call("record_research_sources", getattr(result, "opened_sources", ()))
        record = getattr(result, "research_record", None)
        if record is None:
            return
        summary = None
        to_summary_payload = getattr(record, "to_summary_payload", None)
        if callable(to_summary_payload):
            summary = to_summary_payload()
        elif isinstance(record, Mapping):
            summary = record
        if summary is not None:
            self._sink.call("record_research_record_summary", summary)

    def record_proof_review(self, review: ResearchProofReview | Mapping[str, object] | None) -> None:
        if review is None:
            return
        self._sink.call("record_research_proof_review", proof_review_trace_payload(review))
        self._sink.call("flush")

    def record_plan(self, plan: ResearchPlan | Mapping[str, object] | None) -> None:
        if plan is None:
            return
        self._sink.call("record_research_plan", research_plan_trace_payload(plan))
        self._sink.call("flush")

    def record_evidence_ledger_write(self, result: EvidenceLedgerWriteResult | object | None) -> None:
        if result is None:
            return
        to_trace_payload = getattr(result, "to_trace_payload", None)
        if not callable(to_trace_payload):
            return
        self._sink.call("record_evidence_ledger_write", to_trace_payload())


@dataclass(frozen=True)
class ResearchPipelineConfig:
    enabled: bool = True
    max_followup_rounds: int = 1
    max_queries_per_round: int = 3
    max_sources_per_query: int = 2
    max_total_sources: int = 6
    max_wall_time: float = 90.0
    max_source_preview_chars: int = 2400
    max_followup_context_chars: int = 8000


@dataclass(frozen=True)
class ResearchContext:
    question: str
    session_id: str
    run_id: str
    project: str = ""
    provider_id: str = ""
    proof_question: str = ""
    permission_profile: str = "research"
    max_turns: int = 14
    chat_handoff: str = ""
    should_stop: Callable[[], bool] = lambda: False
    trace: ResearchTraceSink = field(default_factory=NullResearchTraceSink)

    @property
    def effective_proof_question(self) -> str:
        return (
            str(self.proof_question or "").strip()
            or str(self.question or "").strip()
        )


__all__ = [
    "NullResearchTraceSink",
    "ResearchContext",
    "ResearchPipelineConfig",
    "ResearchTraceSink",
    "RunTraceResearchSink",
]
