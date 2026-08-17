from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from codey.research.completion_gate import ResearchCompletionGate
from codey.research.evidence_ledger import EvidenceLedgerStore
from codey.research.ledger import ResearchLedger
from codey.research.object_model import build_research_record
from codey.research.report_quality import review_report_quality
from codey.research.runner import ResearchRunResult


def _record(project: Path | None = None):
    url = "https://example.com/helium"
    source_text = "Helium is separated from natural gas streams. 2026 supply note."
    summary = (
        "## 结论\n"
        "- Helium supply depends on gas processing. [1]\n\n"
        "## 关键证据\n"
        "- [1] The opened source says helium is separated from natural gas streams.\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；需要持续追踪新供应数据。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · fresh · example.com\n\n"
        "## 搜索覆盖\n"
        "- query: helium supply\n"
        "- opened: Helium article\n"
        "- skipped: none representative\n\n"
        "## 来源\n"
        f"[1] Helium article - {url}"
    )
    ledger = ResearchLedger()
    ledger.record_search("helium supply", [{
        "title": "Helium article",
        "url": url,
        "snippet": "Helium supply.",
    }])
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="Helium article",
        text=source_text,
    )
    prepared = ledger.prepare_evidence_items(
        [{
            "claim": "Helium supply depends on gas processing.",
            "source_url": url,
            "excerpt": "Helium is separated from natural gas streams.",
            "stance": "supports",
        }],
        fallback_sources=[url],
        fallback_claim="Helium supply depends on gas processing.",
        fallback_body=source_text,
        note_type="fact",
    )
    assert not prepared.error
    ledger.add_evidence_items(list(prepared.items), note_id="note-1")
    quality = review_report_quality(
        summary,
        ledger=ledger,
        opened_sources=ledger.final_url_set(),
        search_result_urls={url},
    )
    assert quality.ok
    return build_research_record(
        question="Research helium supply",
        summary=summary,
        ledger=ledger,
        review=quality,
        run_id="run-gate",
        session_id="session-gate",
        project=project,
        synthesis_id="synth-gate",
        stop_reason="done",
    )


def _result(record) -> ResearchRunResult:
    return ResearchRunResult(
        "Research helium supply",
        "researched",
        "done",
        1,
        synthesis_id="synth-gate",
        research_record=record,
    )


def test_completion_gate_completes_research_item_with_durable_proof() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project"
        project.mkdir()
        store = EvidenceLedgerStore(Path(td) / "state")
        record = _record(project)
        write = store.append_record(
            record,
            run_id="run-gate",
            session_id="session-gate",
            project=project,
        )
        assert write.ok

        decision = ResearchCompletionGate(store).evaluate(
            item=SimpleNamespace(kind="research", title="Research helium supply"),
            event={"run_id": "run-gate", "stop_reason": "done"},
            research_result=_result(record),
            session_id="session-gate",
            project=project,
        )

    assert decision.complete is True
    assert decision.review is not None
    assert decision.review.ok is True
    assert any(ref.startswith("research_proof:") for ref in decision.proof_refs)
    assert "research:synth-gate" in decision.proof_refs


def test_completion_gate_uses_item_title_over_wrapped_result_question() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "project"
        project.mkdir()
        store = EvidenceLedgerStore(Path(td) / "state")
        record = _record(project)
        write = store.append_record(
            record,
            run_id="run-gate",
            session_id="session-gate",
            project=project,
        )
        assert write.ok
        result = ResearchRunResult(
            (
                "Continue this saved local task.\n"
                "Task: Research helium supply.\n"
                "Reason: Saved local work.\n"
                "Current request: 继续.\n"
                "The current user request overrides this saved task if they conflict."
            ),
            "researched",
            "done",
            1,
            synthesis_id="synth-gate",
            research_record=record,
        )

        decision = ResearchCompletionGate(store).evaluate(
            item=SimpleNamespace(kind="research", title="Research helium supply"),
            event={"run_id": "run-gate", "stop_reason": "done"},
            research_result=result,
            session_id="session-gate",
            project=project,
        )

    assert decision.complete is True
    assert decision.review is not None
    assert decision.review.ok is True
    assert decision.review.proof_ref in decision.proof_refs


def test_completion_gate_blocks_without_research_record() -> None:
    decision = ResearchCompletionGate(None).evaluate(
        item=SimpleNamespace(kind="open_question", title="Research helium supply"),
        event={"run_id": "run-gate", "stop_reason": "done"},
        research_result=ResearchRunResult("q", "summary", "done", 1),
        session_id="session-gate",
        project=None,
    )

    assert decision.block is True
    assert decision.blocked_reason == "research_proof_missing_research_record"


def test_completion_gate_blocks_when_ledger_record_is_unavailable() -> None:
    record = _record()

    decision = ResearchCompletionGate(None).evaluate(
        item=SimpleNamespace(kind="research", title="Research helium supply"),
        event={"run_id": "run-gate", "stop_reason": "done"},
        research_result=_result(record),
        session_id="session-gate",
        project=None,
    )

    assert decision.block is True
    assert decision.review is not None
    assert "missing_evidence_ledger_record" in decision.review.missing_evidence


def test_completion_gate_ignores_non_research_items() -> None:
    decision = ResearchCompletionGate(None).evaluate(
        item=SimpleNamespace(kind="coding", title="Fix parser"),
        event={"run_id": "run-gate", "stop_reason": "done"},
        research_result=None,
    )

    assert decision.no_signal is True
