"""The live matrix distinguishes sequence handoff and real write conflicts."""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.types.json_value import JsonObject
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_full_surface_live_gate as matrix
from tools import browser_prerequisite_ab as browser
from tools.browser_production_live_gate import run_case
from tools.completion_explanation_ab import Target


def test_matrix_preserves_plain_research_and_adds_declared_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def capture(directory: Path, mode: str, target: Target, **options: bool) -> dict[str, object]:
        calls.append({"scenario": directory.name, "mode": mode, **options})
        return {}

    monkeypatch.setattr(matrix, "run_case", capture)
    target = Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10)
    for scenario in ("research", "research_sequence"):
        assert scenario in matrix.SCENARIOS
        matrix._case(tmp_path, scenario, target)
    assert all(row["mode"] == "research" and row["strict_research"] is True for row in calls)
    assert [row["research_sequence"] for row in calls] == [False, True]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("case", ["recover", "invalid_then_recover", "never_legal", "unversioned"])
def test_hybrid_injects_only_at_validated_write_and_reports_real_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    directory = tmp_path / "run"
    correct = ("const r=await tools.read_file({path:'result.json'});"
               "const data=JSON.parse(unwrap(r).text); data.amount=173; data.code='QUARTZ_851';"
               "return unwrap(await tools.edit({path:'result.json',content:JSON.stringify(data),base_result_ref:r.result_ref}));")
    invalid = correct.replace("base_result_ref:r.result_ref", "base_result_ref:unwrap(r).text")
    unversioned = correct.replace("content:JSON.stringify(data),base_result_ref:r.result_ref",
        "replacements:[{old_string:unwrap(r).text,new_string:JSON.stringify(data)}]")

    def replies() -> Iterator[JsonObject]:
        yield browser.code_action(browser.ROUND)
        if case in {"invalid_then_recover", "never_legal"}:
            yield browser.code_action(invalid)
            assert not (directory / "injection.json").exists()
        if case == "unversioned":
            yield browser.code_action(unversioned)
        elif case != "never_legal":
            yield browser.code_action(correct)
            injection = json.loads((directory / "injection.json").read_text(encoding="utf-8"))
            assert injection["trigger"] == "before_version_check"
            yield browser.code_action(correct)
        while True:
            yield {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}}

    install_http(monkeypatch, replies())
    result = run_case(directory, "hybrid", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
                      stale_edit=True, preserve_external_field=True, max_turns=6)
    recovered = case in {"recover", "invalid_then_recover"}
    assert result["passed"] is recovered, result
    assert result["final_file_correct"] is (case != "never_legal")
    assert result["version_check_reached"] is recovered
    assert result["conflict_recovery_covered"] is recovered
    assert result["true_stale_conflicts"] == (1 if recovered else 0)
    assert result["conflict_coverage"] == ("recovered" if recovered else "not_reached")
