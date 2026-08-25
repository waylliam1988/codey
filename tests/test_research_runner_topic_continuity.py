from __future__ import annotations

import tempfile
from pathlib import Path

from codey.knowledge.store import KnowledgeStore
from codey.research.context import ResearchContext, RunTraceResearchSink
from codey.research.pipeline import ResearchIterationRun, ResearchPipeline
from codey.research.runner import ResearchRunner, ResearchRunResult
from codey.research.topic_continuity import project_topic_continuity


class _SectionRecorder:
    """Captures every trace call, with views for sections and topic rows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def record_prompt_section(self, name, text, **kwargs) -> None:
        self.calls.append(("record_prompt_section", (name, text), kwargs))

    def record_research_topic_continuity(self, payload) -> None:
        self.calls.append(("record_research_topic_continuity", (payload,), {}))

    def __getattr__(self, _name):
        def _call(*args, **kwargs) -> None:
            self.calls.append((_name, args, kwargs))
        return _call

    @property
    def sections(self) -> list[dict[str, object]]:
        return [
            {"name": str(args[0]), "text": args[1], **kwargs}
            for method, args, kwargs in self.calls
            if method == "record_prompt_section"
        ]

    @property
    def section_names(self) -> list[str]:
        return [
            str(args[0])
            for method, args, _kwargs in self.calls
            if method == "record_prompt_section"
        ]

    @property
    def context_source_rows(self) -> list[tuple[tuple, dict]]:
        return [
            (args, kwargs)
            for method, args, kwargs in self.calls
            if method == "record_context_sources"
        ]

    @property
    def topic_payloads(self) -> list[dict[str, object]]:
        return [
            dict(args[0])
            for method, args, _kwargs in self.calls
            if method == "record_research_topic_continuity"
        ]


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


def test_runner_admits_topic_continuity_as_dedicated_prompt_section() -> None:
    projection = project_topic_continuity(
        interest_hints=[{
            "ref": "research_interest:ric_x",
            "question": "Does the 2026 finding still hold?",
        }],
    )
    trace = _SectionRecorder()
    runner = _runner(trace, topic_continuity_context=projection.prompt_text)

    intro = runner._intro("Research question about continuity")

    assert "research_topic_continuity" in trace.section_names
    section = next(
        s for s in trace.sections if s["name"] == "research_topic_continuity"
    )
    assert section["purpose"] == "bounded local topic continuity, not evidence"
    assert section["source_refs"] == (
        "local_context:research_topic_continuity",
        "context_source:research_topic_continuity",
    )
    assert "Does the 2026 finding still hold?" in str(section["text"])
    assert "not evidence" in str(section["text"])
    assert "Do not cite this section" in str(section["text"])
    lowered = str(section["text"]).casefold()
    assert not any(term in lowered for term in ("ghost", "work queue"))
    assert intro == "\n\n".join(str(s["text"]) for s in trace.sections)


def test_runner_admission_is_bound_to_one_provider_turn_epoch() -> None:
    projection = project_topic_continuity(
        interest_hints=[{
            "ref": "research_interest:ric_x",
            "question": "Epoch-bound lead?",
        }],
    )
    trace = _SectionRecorder()
    runner = _runner(trace, topic_continuity_context=projection.prompt_text)

    runner._intro("q")

    epochs = {
        str(kwargs.get("epoch_id") or "")
        for _name, _args, kwargs in trace.calls
        if _name == "record_prompt_section"
    }
    assert len(epochs) == 1
    epoch = next(iter(epochs))
    assert epoch.startswith("ctx_epoch:")

    # The admitted source row shares the same provider-turn epoch.
    assert len(trace.context_source_rows) == 1
    (_args, kwargs) = trace.context_source_rows[0]
    sources = _args[0]
    assert [source.key for source in sources] == ["research_topic_continuity"]
    assert kwargs.get("epoch_id") == epoch


def test_runner_without_continuity_keeps_baseline_intro() -> None:
    baseline_trace = _SectionRecorder()
    baseline = _runner(baseline_trace)._intro("Same question")
    baseline_names = [section["name"] for section in baseline_trace.sections]

    enabled_trace = _SectionRecorder()
    enabled = _runner(enabled_trace)._intro("Same question")

    # Empty continuity renders to nothing: identical bytes and no rows.
    assert "research_topic_continuity" not in baseline_names
    assert enabled == baseline
    assert baseline_trace.context_source_rows == []


def test_runner_gate_closes_continuity_even_with_text() -> None:
    from unittest import mock

    trace = _SectionRecorder()
    runner = _runner(
        trace,
        topic_continuity_context="Local research continuity. This is not evidence.",
    )

    with mock.patch(
        "codey.research.runner.allows_context_source",
        return_value=False,
    ):
        runner._intro("q")

    assert "research_topic_continuity" not in trace.section_names
    assert trace.context_source_rows == []


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

    runner._intro("q")

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
        "items": [{"refs": ["research_interest:ric_x"], "kind": "open_question", "stale": False}],
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


def test_run_trace_persists_digest_only_topic_continuity_row() -> None:
    import json

    from codey.run_trace import RunTraceStore
    from codey.research.topic_continuity import project_topic_continuity

    projection = project_topic_continuity(
        interest_hints=[{
            "ref": "research_interest:ric_x",
            "question": "Does the sensitive copper claim still hold?",
        }],
        claim_refs=("prior-claim-1",),
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store = RunTraceStore(td)
        recorder = store.open(
            run_id="run-tc",
            session_id="s-tc",
            project=None,
            mode_initial="research",
            provider_initial="deepseek",
        )
        RunTraceResearchSink(recorder).record_topic_continuity(
            projection.to_payload()
        )
        recorder.finish(status="done")
        manifest = json.loads(
            store.path_for("s-tc", "run-tc").read_text(encoding="utf-8")
        )

    rows = manifest["research_topic_continuity"]
    assert len(rows) == 1
    row = rows[0]
    assert row["digest"] == projection.to_payload()["digest"]
    assert row["context_source"] == "research_topic_continuity"
    assert row["item_count"] == 2
    assert row["claim_ref_count"] == 1
    kinds = sorted(item["kind"] for item in row["items"])
    assert kinds == ["open_question", "prior_claim"]
    refs = [ref for item in row["items"] for ref in item["refs"]]
    assert "research_interest:ric_x" in refs
    raw = json.dumps(manifest, ensure_ascii=False)
    assert "Does the sensitive copper claim still hold?" not in raw


def test_run_trace_dedupes_and_fails_closed_on_missing_digest() -> None:
    import json

    from codey.run_trace import RunTraceStore
    from codey.research.topic_continuity import project_topic_continuity

    payload = project_topic_continuity(
        interest_hints=[{"ref": "r1", "question": "Lead?"}],
    ).to_payload()
    digest = payload["digest"]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store = RunTraceStore(td)
        recorder = store.open(
            run_id="run-dup",
            session_id="s-dup",
            project=None,
            mode_initial="research",
            provider_initial="deepseek",
        )
        recorder.record_research_topic_continuity(payload)
        recorder.record_research_topic_continuity(payload)  # dedupe by digest
        recorder.record_research_topic_continuity({"admitted": True})  # no digest
        recorder.record_research_topic_continuity({**payload, "admitted": False})
        recorder.flush()
        manifest = json.loads(
            store.path_for("s-dup", "run-dup").read_text(encoding="utf-8")
        )

    rows = manifest["research_topic_continuity"]
    assert len(rows) == 1
    assert rows[0]["digest"] == digest
