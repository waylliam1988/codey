"""Narrow Research pipeline context and trace projection helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Protocol

from codey.prompt_envelope import FailOpenPromptTrace
from codey.research.evidence_ledger import EvidenceLedgerWriteResult
from codey.research.proof_quality import ResearchProofReview, proof_review_trace_payload
from codey.research.query_planner import ResearchPlan, research_plan_trace_payload
from codey.research.review_finding import (
    planner_gap_trace_payloads,
    review_finding_trace_payloads,
)


class ResearchTraceSink(Protocol):
    def record_result(self, result: object) -> None:
        ...

    def record_pipeline_result(self, result: object) -> None:
        ...

    def record_proof_review(self, review: ResearchProofReview | Mapping[str, object] | None) -> None:
        ...

    def record_plan(self, plan: ResearchPlan | Mapping[str, object] | None) -> None:
        ...

    def record_evidence_ledger_write(self, result: EvidenceLedgerWriteResult | object | None) -> None:
        ...

    def record_review_findings(self, findings: Iterable[object] | None) -> None:
        ...

    def record_planner_gaps(self, gaps: Iterable[object] | None) -> None:
        ...

    def record_research_source_trust(self, projections: Iterable[object] | None) -> None:
        ...

    def record_research_brief_projection(self, projection: Mapping[str, object] | None) -> None:
        ...

    def record_topic_continuity(self, projection: Mapping[str, object] | object | None) -> None:
        ...


class NullResearchTraceSink:
    def record_result(self, result: object) -> None:
        del result

    def record_pipeline_result(self, result: object) -> None:
        del result

    def record_proof_review(self, review: ResearchProofReview | Mapping[str, object] | None) -> None:
        del review

    def record_plan(self, plan: ResearchPlan | Mapping[str, object] | None) -> None:
        del plan

    def record_evidence_ledger_write(self, result: EvidenceLedgerWriteResult | object | None) -> None:
        del result

    def record_review_findings(self, findings: Iterable[object] | None) -> None:
        del findings

    def record_planner_gaps(self, gaps: Iterable[object] | None) -> None:
        del gaps

    def record_research_source_trust(self, projections: Iterable[object] | None) -> None:
        del projections

    def record_research_brief_projection(self, projection: Mapping[str, object] | None) -> None:
        del projection

    def record_topic_continuity(self, projection: Mapping[str, object] | object | None) -> None:
        del projection


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

    def record_pipeline_result(self, result: object) -> None:
        to_payload = getattr(result, "to_payload", None)
        payload = to_payload() if callable(to_payload) else result
        if isinstance(payload, Mapping):
            self._sink.call("record_research_pipeline_result", payload)
            self._sink.call("flush")

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

    def record_review_findings(self, findings: Iterable[object] | None) -> None:
        payloads = review_finding_trace_payloads(findings or ())
        if not payloads:
            return
        self._sink.call("record_review_findings", payloads)
        self._sink.call("flush")

    def record_planner_gaps(self, gaps: Iterable[object] | None) -> None:
        payloads = planner_gap_trace_payloads(gaps or ())
        if not payloads:
            return
        self._sink.call("record_planner_gaps", payloads)
        self._sink.call("flush")

    def record_research_source_trust(self, projections: Iterable[object] | None) -> None:
        payloads = [
            item.to_payload() if callable(getattr(item, "to_payload", None)) else item
            for item in (projections or ())
        ]
        payloads = [item for item in payloads if isinstance(item, Mapping)]
        if not payloads:
            return
        self._sink.call("record_research_source_trust", payloads)
        self._sink.call("flush")

    def record_research_brief_projection(self, projection: Mapping[str, object] | None) -> None:
        to_payload = getattr(projection, "to_payload", None)
        payload = to_payload() if callable(to_payload) else projection
        if not isinstance(payload, Mapping):
            return
        self._sink.call("record_research_brief_projection", payload)
        self._sink.call("flush")

    def record_topic_continuity(self, projection: Mapping[str, object] | object | None) -> None:
        to_payload = getattr(projection, "to_payload", None)
        payload = to_payload() if callable(to_payload) else projection
        if not isinstance(payload, Mapping):
            return
        if not payload.get("admitted"):
            return
        self._sink.call("record_research_topic_continuity", payload)
        self._sink.call("flush")


@dataclass(frozen=True)
class ResearchPipelineConfig:
    enabled: bool = True
    max_followup_rounds: int = 1
    max_queries_per_round: int = 3
    max_sources_per_query: int = 2
    max_total_sources: int = 6
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
    topic_continuity_context: str = ""
    topic_continuity_payload: Mapping[str, object] | None = None
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
