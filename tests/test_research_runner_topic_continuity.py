from __future__ import annotations

import tempfile
from pathlib import Path

from codey.knowledge.store import KnowledgeStore
from codey.research.context import ResearchContext, RunTraceResearchSink
from codey.research.pipeline import ResearchIterationRun, ResearchPipeline
from codey.research.runner import ResearchRunner, ResearchRunResult
from codey.research.topic_continuity import project_topic_continuity


class _SectionRecorder:
    """Captures prompt-envelope sections and topic rows projected to trace."""

    def __init__(self) -> None:
        self.sections: list[dict[str, object]] = []
        self.topic_payloads: list[dict[str, object]] = []

    def record_prompt_section(self, name, text, **kwargs) -> None:
        self.sections.append({"name": name, "text": text, **kwargs})

    def record_research_topic_continuity(self, payload) -> None:
        self.topic_payloads.append(dict(payload))

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _NullSearch:
    def search(self, *_args, **_kwargs) -> list[dict]:
        return []


def _runner(trace, *, topic_continuity_context: str = "") -> ResearchRunner:
    store = KnowledgeStore(Path(tempfile.mkdtemp()) / "knowledge")
    return ResearchRunner(
        provider=None,
        search=_NullSearch(),
        store=store,
        session_id="s-continuity",
        trace_recorder=trace,
        topic_continuity_context=topic_continuity_context,
    )


def _rendered_intro(trace: _SectionRecorder, runner: ResearchRunner, question: str) -> str:
    intro = runner._intro(question)
    assert intro == "\n\n".join(str(s["text"]) for s in trace.sections)
    return intro


def test_runner_admits_topic_continuity_as_dedicated_prompt_section() -> None:
    projection = project_topic_continuity(
        interest_hints=[{
            "ref": "research_interest:ric_x",
            "question": "Does the 2026 finding still hold?",
        }],
    )
    trace = _SectionRecorder()
    runner = _runner(trace, topic_continuity_context=projection.prompt_text)

    _rendered_intro(trace, runner, "Research question about continuity")

    names = [section["name"] for section in trace.sections]
    assert "research_topic_continuity" in names
    section = next(s for s in trace.sections if s["name"] == "research_topic_continuity")
    assert section["purpose"] == "bounded local topic continuity, not evidence"
    assert section["source_refs"] == ("local_context:research_topic_continuity",)
    assert "Does the 2026 finding still hold?" in str(section["text"])
    assert "not evidence" in str(section["text"])
    assert "Do not cite this section" in str(section["text"])
    lowered = str(section["text"]).casefold()
    assert not any(term in lowered for term in ("ghost", "work queue"))


def test_runner_without_continuity_keeps_baseline_intro() -> None:
    baseline_trace = _SectionRecorder()
    baseline = _rendered_intro(baseline_trace, _runner(baseline_trace), "Same question")
    baseline_names = [section["name"] for section in baseline_trace.sections]

    enabled_trace = _SectionRecorder()
    enabled = _rendered_intro(
        enabled_trace,
        _runner(enabled_trace, topic_continuity_context=""),
        "Same question",
    )

    # Empty continuity is skipped by the envelope renderer: identical bytes.
    assert "research_topic_continuity" not in baseline_names
    assert enabled == baseline


def test_runner_iteration_context_and_continuity_stay_separate() -> None:
    trace = _SectionRecorder()
    store = KnowledgeStore(Path(tempfile.mkdtemp()) / "knowledge")
    runner = ResearchRunner(
        provider=None,
        search=_NullSearch(),
        store=store,
        session_id="s",
        trace_recorder=trace,
        iteration_context="follow-up material only",
        topic_continuity_context="Local research continuity. This is not evidence.\n- lead one",
    )

    _rendered_intro(trace, runner, "q")

    sections = {s["name"]: str(s["text"]) for s in trace.sections}
    assert "follow-up material" in sections["research_iteration_context"]
    assert "not evidence" in sections["research_topic_continuity"]
    assert "lead one" not in sections["research_iteration_context"]
    assert "follow-up material" not in sections["research_topic_continuity"]


def _stub_result() -> ResearchRunResult:
    return ResearchRunResult(
        question="question",
        summary="",
        stop_reason="done",
        turns=1,
    )


def test_pipeline_forwards_continuity_to_initial_iteration_and_trace() -> None:
    trace = _SectionRecorder()
    received: dict[str, object] = {}
    payload = {
        "schema_version": 1,
        "kind": "research_topic_continuity_projection",
        "context_source": "research_topic_continuity",
        "admitted": True,
        "item_count": 1,
        "candidate_count": 1,
        "claim_ref_count": 0,
        "truncated": False,
        "reason_codes": ["interest_lead"],
        "warnings": [],
        "items": [{"ref": "research_interest:ric_x", "kind": "open_question", "stale": False}],
        "candidates": [{"candidate_id": "topic_1"}],
        "digest": "sha256:" + "0" * 64,
    }

    def run_iteration(**kwargs):
        received.update(kwargs)
        return ResearchIterationRun(result=_stub_result())

    context = ResearchContext(
        question="Continuity question",
        session_id="s1",
        run_id="run-1",
        max_turns=2,
        trace=RunTraceResearchSink(trace),
        topic_continuity_context="Local research continuity. This is not evidence.",
        topic_continuity_payload=payload,
    )

    output = ResearchPipeline(
        context=context,
        run_iteration=run_iteration,
        search_factory=_NullSearch,
    ).run()

    assert output.final_result is not None
    assert received.get("topic_continuity_context") == (
        "Local research continuity. This is not evidence."
    )
    assert trace.topic_payloads == [payload]


def test_pipeline_skips_trace_row_when_nothing_admitted() -> None:
    trace = _SectionRecorder()

    def run_iteration(**_kwargs):
        return ResearchIterationRun(result=_stub_result())

    context = ResearchContext(
        question="Baseline question",
        session_id="s1",
        run_id="run-2",
        max_turns=2,
        trace=RunTraceResearchSink(trace),
    )

    output = ResearchPipeline(
        context=context,
        run_iteration=run_iteration,
        search_factory=_NullSearch,
    ).run()

    assert output.planner_stop_reason == "proof_review_missing"
    assert trace.topic_payloads == []
