from __future__ import annotations

import tempfile
from pathlib import Path

from codey.knowledge.store import KnowledgeStore
from codey.operations.research_iteration import ResearchIteration
from codey.research.context import ResearchContext, RunTraceResearchSink
from codey.research.pipeline import ResearchIterationRun, ResearchPipeline
from codey.research.run_result import ResearchRunResult
from codey.research.topic_continuity import project_topic_continuity
from codey.workspace.context_epoch import context_epoch_id


class _SectionRecorder:
    """Captures every trace call, with views for sections and topic rows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def record_prompt_section(self, name, text, **kwargs) -> None:
        self.calls.append(("record_prompt_section", (name, text), kwargs))

    def record_research_topic_continuity(self, payload, **kwargs) -> None:
        self.calls.append(("record_research_topic_continuity", (payload,), kwargs))

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


class _FakeProvider:
    name = "Fake Provider"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def new_chat(self, timeout=None) -> None:
        del timeout

    def send(self, text: str, timeout=None) -> str:
        del timeout
        self.prompts.append(text)
        return "{}"


class _RecordingRunner(ResearchIteration):
    """Exposes the last assembled intro for byte-level assertions."""

    def __init__(self, *args, **kwargs) -> None:
        self.last_intro = ""
        super().__init__(*args, **kwargs)

    def _intro(self, question: str) -> str:
        self.last_intro = super()._intro(question)
        return self.last_intro


def _runner(trace, *, topic_continuity_context: str = "") -> _RecordingRunner:
    store = KnowledgeStore(Path(tempfile.mkdtemp()) / "knowledge")
    return _RecordingRunner(
        _FakeProvider(),
        _NullSearch(),
        store,
        session_id="s-continuity",
        trace_recorder=trace,
        topic_continuity_context=topic_continuity_context,
    )


def _send(runner: _RecordingRunner, *, controller_block: str = "") -> str:
    """Drive one real provider turn; the block mimics the controller append."""
    outbound = runner.last_intro + controller_block
    runner._send_provider(outbound)
    return outbound


def _stub_result() -> ResearchRunResult:
    return ResearchRunResult(
        question="question",
        summary="",
        stop_reason="done",
        turns=1,
    )


def test_continuity_appears_in_prompt_via_new_entry() -> None:
    """Continuity via the new single entry (migrated from old _intro)."""
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    from codey.knowledge.store import KnowledgeStore
    from codey.operations.research_iteration import run_research_iteration

    class _CaptureProvider:
        def __init__(self, replies: list[str]) -> None:
            self._replies = list(replies)
            self.sent: list[str] = []

        def new_chat(self, timeout: object = None) -> None:
            return None

        def send(self, prompt: str, timeout: object = None) -> str:
            self.sent.append(str(prompt))
            return self._replies.pop(0) if self._replies else '{"tool":"done","args":{"summary":"done"}}'

        def close(self) -> None:
            return None

    projection = project_topic_continuity(
        interest_hints=[{"ref": "research_interest:ric_x", "question": "Does the 2026 finding still hold?"}],
    )
    with tempfile.TemporaryDirectory() as td:
        store = KnowledgeStore(Path(td) / "knowledge")
        provider = _CaptureProvider(['{"tool":"done","args":{"summary":"done"}}'])
        deps = SimpleNamespace(
            knowledge_store=store,
            search_factory=lambda: (lambda *a, **k: ""),
            managed_outputs=None,
            runtime_mutations=None,
        )
        run_research_iteration(
            deps, provider=provider, session_id="s", project="",
            task="Research question about continuity", max_turns=1,
            on_event=lambda _e: None, stop_flag=None, provider_id="local",
            run_id="r", chat_handoff="", trace_recorder=None,
            search=lambda: "", topic_continuity_context=projection.prompt_text,
        )
        store.close()
    assert provider.sent
    assert "Does the 2026 finding still hold?" in provider.sent[0]
    assert "Research question about continuity" in provider.sent[0]


def test_pipeline_forwards_continuity_payload_without_pre_recording() -> None:
    """The pipeline hands the payload through; the runner owns the row.

    An admitted trace row must be bound to outbound provider-send attempt
    bytes, so recording happens at the send boundary, never before it.
    """
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
    assert received.get("topic_continuity_payload") == payload
    # The pipeline itself stays silent; no send happened here.
    assert trace.topic_payloads == []


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

    from codey.runs.trace import RunTraceStore

    projection = project_topic_continuity(
        interest_hints=[{
            "ref": "research_interest:ric_x",
            "question": "Does the sensitive copper claim still hold?",
        }],
        claim_refs=("prior-claim-1",),
    )
    epoch = context_epoch_id("outbound intro + controller action block")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store = RunTraceStore(td)
        recorder = store.open(
            run_id="run-tc",
            session_id="s-tc",
            project=None,
            mode_initial="research",
            provider_initial="deepseek",
        )
        recorder.record_research_topic_continuity(
            projection.to_payload(),
            epoch_id=epoch,
        )
        recorder.finish(status="done")
        manifest = json.loads(
            store.path_for("s-tc", "run-tc").read_text(encoding="utf-8")
        )

    rows = manifest["research_topic_continuity"]
    assert len(rows) == 1
    row = rows[0]
    assert row["digest"] == projection.to_payload()["digest"]
    assert row["epoch_id"] == epoch
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

    from codey.runs.trace import RunTraceStore

    payload = project_topic_continuity(
        interest_hints=[{"ref": "r1", "question": "Lead?"}],
    ).to_payload()
    digest = payload["digest"]
    epoch = context_epoch_id("outbound bytes")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store = RunTraceStore(td)
        recorder = store.open(
            run_id="run-dup",
            session_id="s-dup",
            project=None,
            mode_initial="research",
            provider_initial="deepseek",
        )
        recorder.record_research_topic_continuity(payload, epoch_id=epoch)
        recorder.record_research_topic_continuity(payload, epoch_id=epoch)  # dedupe by digest
        recorder.record_research_topic_continuity({"admitted": True}, epoch_id=epoch)  # no digest
        recorder.record_research_topic_continuity({**payload, "admitted": False}, epoch_id=epoch)
        recorder.flush()
        manifest = json.loads(
            store.path_for("s-dup", "run-dup").read_text(encoding="utf-8")
        )

    rows = manifest["research_topic_continuity"]
    assert len(rows) == 1
    assert rows[0]["digest"] == digest


def test_topic_admission_has_no_bypass_outside_the_send_boundary() -> None:
    """The sink exposes no continuity writer; the recorder demands an epoch.

    Together these make "admitted row exists" structurally imply "bound to
    outbound provider-send attempt bytes": no caller can project an
    un-bound row, so the runner's gate and sent-bytes binding cannot be
    bypassed. (Format validation of the epoch itself lives in
    test_topic_admission_rejects_empty_or_malformed_epoch.)
    """
    import pytest

    from codey.runs.trace import RunTraceStore

    # The projection sink has no continuity writer at all.
    assert not hasattr(RunTraceResearchSink(None), "record_topic_continuity")

    payload = {
        "schema_version": 1,
        "context_source": "research_topic_continuity",
        "admitted": True,
        "digest": "sha256:" + "3" * 64,
    }
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store = RunTraceStore(td)
        recorder = store.open(
            run_id="run-no-epoch",
            session_id="s-no-epoch",
            project=None,
            mode_initial="research",
            provider_initial="deepseek",
        )
        with pytest.raises(TypeError):
            recorder.record_research_topic_continuity(payload)


def test_topic_admission_rejects_empty_or_malformed_epoch() -> None:
    """The trace boundary fails closed unless the epoch is ctx_epoch:<16 hex>.

    An empty or malformed ref writes no row and never touches the dedupe
    key, so the payload stays admissible by a later valid send — while a
    well-formed sent-bytes ref admits exactly one row.
    """
    import json

    from codey.runs.trace import RunTraceStore

    payload = project_topic_continuity(
        interest_hints=[{"ref": "r1", "question": "Lead?"}],
    ).to_payload()
    rejected_epochs = (
        "",
        "not-an-epoch",
        "ctx_epoch:",
        "ctx_epoch:" + "x" * 16,  # not hex
        "ctx_epoch:" + "a" * 15,  # too short
        "ctx_epoch:" + "A" * 16,  # uppercase is a foreign vocabulary
        "sha256:" + "a" * 64,
    )
    for index, bad in enumerate(rejected_epochs):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            store = RunTraceStore(td)
            recorder = store.open(
                run_id=f"run-bad-epoch-{index}",
                session_id="s-bad-epoch",
                project=None,
                mode_initial="research",
                provider_initial="deepseek",
            )
            recorder.record_research_topic_continuity(payload, epoch_id=bad)
            recorder.flush()
            manifest = json.loads(
                store.path_for("s-bad-epoch", f"run-bad-epoch-{index}").read_text(
                    encoding="utf-8"
                )
            )
            assert manifest["research_topic_continuity"] == []

    # A valid sent-bytes ref admits the row the rejected attempts left
    # unwritten: same recorder, dedupe key was never polluted.
    good = context_epoch_id("outbound bytes")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store = RunTraceStore(td)
        recorder = store.open(
            run_id="run-good-epoch",
            session_id="s-good-epoch",
            project=None,
            mode_initial="research",
            provider_initial="deepseek",
        )
        for bad in ("", good):
            recorder.record_research_topic_continuity(payload, epoch_id=bad)
        recorder.flush()
        manifest = json.loads(
            store.path_for("s-good-epoch", "run-good-epoch").read_text(encoding="utf-8")
        )

    rows = manifest["research_topic_continuity"]
    assert len(rows) == 1
    assert rows[0]["epoch_id"] == good
