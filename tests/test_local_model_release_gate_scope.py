"""Release profile cannot be reduced to one passing case."""

from __future__ import annotations

from tools.gate_policy import scope_verdict


def test_release_scope_rejects_single_selected_case() -> None:
    verdict = scope_verdict(
        {"ok": True, "results": [{"case": "chat", "ok": True}]},
        profile="release",
        required_cases=("chat", "hybrid"),
        selected_cases=("chat",),
    )
    assert verdict["release_ready"] is False
    assert verdict["missing_release_cases"] == ["hybrid"]


def test_release_scope_accepts_complete_matrix_only_when_summary_passes() -> None:
    verdict = scope_verdict(
        {"ok": True}, profile="release",
        required_cases=("chat", "hybrid"), selected_cases=("chat", "hybrid"),
    )
    assert verdict["release_ready"] is True
