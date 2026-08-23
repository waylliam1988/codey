from __future__ import annotations

import json
from pathlib import Path

from tests.manual import research_to_code_ab as probe


def test_research_to_code_ab_self_test() -> None:
    probe._self_test()


def test_arm_briefs_differ_only_in_rendering_not_facts() -> None:
    briefs = probe._arm_briefs()

    for arm, brief in briefs.items():
        assert probe.TRAP_TOKEN in brief, arm
        assert probe.KEY_FORMULA_TOKEN.replace(" ", "") in brief.replace(" ", ""), arm
        assert "Research context from this chat" in brief, arm
    # Baseline carries the raw excerpt + related-id noise; the projection arm
    # carries neither.
    assert "Synthesis excerpt" in briefs["baseline"]
    assert "Synthesis excerpt" not in briefs["projection"]
    assert probe.RELATED_NOTE_ID in briefs["baseline"]
    assert probe.RELATED_NOTE_ID not in briefs["projection"]
    assert len(briefs["baseline"]) > len(briefs["projection"])


def test_score_arm_detects_applied_formula_and_trap_misuse(tmp_path: Path) -> None:
    good = tmp_path / "pricing.py"
    good.write_text(
        "def discounted_total(amount, discount, tax_rate):\n"
        "    return (amount - discount) * (1 + tax_rate)\n",
        encoding="utf-8",
    )
    scores = probe.score_arm(
        summary="done",
        changed_files=("pricing.py",),
        root=tmp_path,
    )
    assert scores == {"key_conclusion_applied": True, "trap_misused": False}

    bad = tmp_path / "notes.py"
    bad.write_text(
        f"# TODO: prepare for {probe.TRAP_TOKEN}\n", encoding="utf-8"
    )
    trap_scores = probe.score_arm(
        summary="added migration note",
        changed_files=("notes.py",),
        root=tmp_path,
    )
    assert trap_scores["trap_misused"] is True

    untouched = tmp_path / "pricing.py"
    untouched.write_text(
        "def discounted_total(amount, discount, tax_rate):\n"
        "    return amount * (1 + tax_rate) - discount\n",
        encoding="utf-8",
    )
    wrong_order = probe.score_arm(
        summary="kept tax-first formula",
        changed_files=("pricing.py",),
        root=tmp_path,
    )
    assert wrong_order["key_conclusion_applied"] is False


def test_summarize_reports_projection_delta_vs_baseline() -> None:
    rows = [
        {
            "case": "discount-before-tax",
            "arm": "baseline",
            "success": True,
            "key_conclusion_applied": True,
            "trap_misused": False,
            "independent_check_passed": True,
            "sent_chars": 9000,
            "brief_chars": 1500,
            "turns": 5,
            "tool_calls": 4,
            "protocol_errors": 0,
        },
        {
            "case": "discount-before-tax",
            "arm": "projection",
            "success": True,
            "key_conclusion_applied": True,
            "trap_misused": False,
            "independent_check_passed": True,
            "sent_chars": 7000,
            "brief_chars": 900,
            "turns": 5,
            "tool_calls": 4,
            "protocol_errors": 0,
        },
    ]

    summary = probe._summarize(rows)

    delta = summary["projection_delta_vs_baseline"]
    assert delta["brief_chars"] == 900 - 1500
    assert delta["sent_chars"] < 0
    assert delta["success"] == 0
    assert delta["trap_misused"] == 0
    serialized = json.dumps(summary)
    assert "projection_delta_vs_baseline" in serialized


def test_arm_schedule_interleaves_to_cancel_order_bias() -> None:
    one_repeat = probe._arm_schedule(1)
    two_repeats = probe._arm_schedule(2)

    assert [arm for arm, _ in one_repeat] == ["baseline", "projection"]
    assert [arm for arm, _ in two_repeats] == [
        "baseline",
        "projection",
        "projection",
        "baseline",
    ]
    assert [repeat for _, repeat in two_repeats] == [1, 1, 2, 2]


def _row(arm: str, **overrides: object) -> dict:
    row = {
        "case": "discount-before-tax",
        "arm": arm,
        "success": True,
        "key_conclusion_applied": True,
        "trap_misused": False,
        "independent_check_passed": True,
    }
    row.update(overrides)
    return row


def test_gate_verdict_passes_when_projection_matches_baseline() -> None:
    rows = [_row("baseline"), _row("projection")]

    verdict = probe._gate_verdict(rows)

    assert verdict["ok"] is True
    assert all(verdict["criteria"].values())


def test_gate_verdict_fails_when_projection_regresses_or_trap_fires() -> None:
    regressed = probe._gate_verdict([
        _row("baseline"),
        _row("projection", success=False),
    ])
    assert regressed["ok"] is False
    assert regressed["criteria"]["success_not_worse"] is False

    trap = probe._gate_verdict([
        _row("baseline"),
        _row("projection", trap_misused=True),
    ])
    assert trap["ok"] is False
    assert trap["criteria"]["trap_misuse_not_worse"] is False

    errored = probe._gate_verdict([
        _row("baseline"),
        _row("projection"),
        {"case": "x", "arm": "projection", "error": "boom"},
    ])
    assert errored["ok"] is False
    assert errored["criteria"]["no_error_rows"] is False

    empty = probe._gate_verdict([])
    assert empty["ok"] is False
    assert empty["criteria"]["arms_populated"] is False


def test_tracing_provider_journals_send_reply_and_transcript(tmp_path) -> None:
    from tests.manual.ab_journal import (
        ABJournalWriter,
        TranscriptReplayCache,
        TRANSCRIPT_MODE_ARCHIVE,
        verify_event_chain,
    )

    journal_dir = tmp_path / "journal"
    journal = ABJournalWriter(
        directory=journal_dir,
        experiment_id="research_to_code_ab",
        run_id="unit-test",
        provider="echo",
        transcript_cache=TranscriptReplayCache(journal_dir, mode=TRANSCRIPT_MODE_ARCHIVE),
    )

    class _EchoProvider:
        id = "echo"
        name = "Echo"

        def new_chat(self, timeout=None):
            return object()

        def send(self, text, timeout=None):
            return "ok-reply"

        def close(self):
            return None

    provider = probe.TracingProvider(
        _EchoProvider(),
        timeout=10.0,
        new_chat_timeout=5.0,
        journal=journal,
        case="case-a",
        arm="baseline",
    )

    reply = provider.send("hello prompt")

    assert reply == "ok-reply"
    assert provider.prompts == ["hello prompt"]
    assert provider.replies == ["ok-reply"]
    journal.close()

    events = [
        json.loads(line)
        for line in (journal_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = [event.get("event_type") or event.get("type") for event in events]
    assert any(kind == "send_start" for kind in kinds), kinds
    assert any(kind == "reply" for kind in kinds), kinds
    assert verify_event_chain(events) == []
    transcripts = list((journal_dir / "transcripts").glob("*.json"))
    assert transcripts, "archive mode must store the prompt/reply pair"
    payload = json.loads(transcripts[0].read_text(encoding="utf-8"))
    assert payload["prompt"] == "hello prompt"
    assert payload["reply"] == "ok-reply"


def test_tracing_provider_without_journal_still_counts(tmp_path=None) -> None:
    class _EchoProvider:
        id = "echo"
        name = "Echo"

        def new_chat(self, timeout=None):
            return object()

        def send(self, text, timeout=None):
            return "r1"

        def close(self):
            return None

    provider = probe.TracingProvider(_EchoProvider(), timeout=10.0, new_chat_timeout=5.0)

    provider.send("a")

    assert provider.prompts == ["a"]
    assert provider.replies == ["r1"]
