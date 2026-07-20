from __future__ import annotations

import tempfile
from pathlib import Path

from codey.review_impact_map import render_review_impact_map
from tests.manual import review_impact_map_ab


def test_review_impact_map_finds_external_caller_and_test_without_source_body() -> None:
    case = next(
        item
        for item in review_impact_map_ab.CASES
        if item.name == "ts-exported-caller-and-test"
    )

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        review_impact_map_ab._write_fixture(root, case.files)
        impact_map = render_review_impact_map(root, case.changes)

    assert "Review Impact Map" in impact_map
    assert "formatTotal -> formatCurrency" in impact_map
    assert "src/view.ts:" in impact_map
    assert "tests/format.test.ts:" in impact_map
    assert "return `Total:" not in impact_map
    assert "expect(formatTotal" not in impact_map


def test_current_prompt_does_not_include_probe_impact_map() -> None:
    case = review_impact_map_ab.CASES[0]

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        review_impact_map_ab._write_fixture(root, case.files)
        current = review_impact_map_ab._prompt_for(root, case, "current")
        impact = review_impact_map_ab._prompt_for(root, case, "impact_map")

    assert "ChangeSet Summary" in current
    assert "Review Impact Map" not in current
    assert "Review Impact Map" in impact


def test_private_contained_case_does_not_score_as_false_positive() -> None:
    case = next(
        item
        for item in review_impact_map_ab.CASES
        if item.name == "private-helper-contained"
    )
    row = review_impact_map_ab._self_test_row(case, "impact_map")

    assert row["expected_verdict"] == "approved"
    assert row["verdict"] == "approved"
    assert not row["false_positive_review"]
