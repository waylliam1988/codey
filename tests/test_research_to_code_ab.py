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
