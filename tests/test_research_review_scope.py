"""Frozen task/claim coverage and literal quote bounds, without semantic certification."""

import copy
import json

import pytest

from codey.research.semantic_review import parse_review, review_issues
from codey.research.task_requirements import task_requirements, text_units


@pytest.mark.parametrize("text", [
    "Compare current guarantees, old limits; measure workload.\nReport in Chinese.",
    "\u5f53\u524d\u4fdd\u8bc1\uff0c\u65e7\u7248\u672c\u9650\u5236\uff0c\u5de5\u4f5c\u8d1f\u8f7d\u3002\n\u4e2d\u6587\u7ed3\u8bba\u3002",
    "Read https://www.sqlite.org/wal.html and v3.22.0. Preserve 0.5 ms.",
])
def test_text_scope_covers_every_nonblank_character_and_keeps_original_offsets(text):
    units = text_units(text)
    covered = set()
    for row in units:
        assert row["text"] == text[row["start"]:row["end"]]
        covered.update(range(row["start"], row["end"]))
    assert all(index in covered for index, char in enumerate(text) if not char.isspace())
    assert task_requirements(text) == task_requirements(text)
    assert [row["task_ref"] for row in task_requirements(text)] == [f"r{n}" for n in range(1, len(units) + 1)]
    assert any("https://www.sqlite.org/wal.html" in row["text"] for row in units) == ("https://" in text)


@pytest.mark.parametrize("text", [
    "在 WAL 模式下，同一时刻只能有一个写者。",
    "Before 3.22.0, read-only opening was impossible; since 3.22.0, specific conditions apply.",
    "In mice, A beat B, but C was not evaluated.",
])
def test_review_sentence_keeps_conditions_subject_and_comparator_together(text):
    from codey.research.semantic_review import claim_units

    units = claim_units(text, {})
    assert units == [{"unit": 1, "start": 0, "end": len(text), "text": text}]
    assert len(task_requirements(text)) > 1


def case():
    task = "Explain checkpoint defaults. Distinguish old restrictions."
    claim = "Checkpoint defaults to 1000 pages. FULL waits for readers."
    basis = {"task": task, "requirements": task_requirements(task), "questions": ["Checkpoint defaults?", "Old restrictions?"],
        "claims": [{"ref": "f1", "units": text_units(claim),
                    "evidence": [{"excerpt": "By default the threshold is 1000 pages."}]}],
        "paragraphs": [{"path": "summary", "refs": ["f1"], "units": text_units("1000 pages. FULL waits.")}],
        "coverage": [{"question_ref": "q1", "state": "evidence_found", "investigations": ["a1"]},
                     {"question_ref": "q2", "state": "not_investigated", "investigations": []}]}
    assessment = {"requirements": [
        {"task_ref": "r1", "question_refs": ["q1"], "report_paths": ["summary"], "state": "addressed", "reason": "Threshold stated."},
        {"task_ref": "r2", "question_refs": ["q2"], "report_paths": ["summary"], "state": "acknowledged_gap", "reason": "History remains unknown."}],
        "investigations": [{"ref": "a1", "question_ref": "q1", "verdict": "relevant", "reason": "Opened checkpoint documentation."}],
        "claims": [{"ref": "f1", "units": [
            {"unit": 1, "verdict": "supported", "quotes": [{"evidence": 1, "quote": "threshold is 1000 pages"}], "reason": "Explicit default."},
            {"unit": 2, "verdict": "unsupported", "quotes": [], "reason": "No FULL behavior in selected evidence."}]}],
        "paragraphs": [{"path": "summary", "units": [
            {"unit": 1, "verdict": "supported", "basis": "findings", "finding_units": [{"ref": "f1", "unit": 1}],
             "reason": "Explicit default."},
            {"unit": 2, "verdict": "unsupported", "basis": "findings", "finding_units": [],
             "reason": "An extra mode is unsupported."}]}]}
    return basis, assessment


def test_supported_main_point_does_not_hide_rejected_extra_claim_unit():
    basis, assessment = case()
    parsed = parse_review(json.dumps(assessment), basis)
    assert {row["kind"] for row in review_issues(parsed)} == {"claim_support", "report_scope"}
    issue = next(row for row in review_issues(parsed) if row["kind"] == "claim_support")
    assert issue["ref"] == "f1" and issue["unit"] == 2


@pytest.mark.parametrize("quote", ["threshold is 1000 pages", "threshold\nis\t1000 pages"])
def test_quote_layout_whitespace_does_not_change_the_selected_contiguous_words(quote):
    basis, assessment = case()
    basis["claims"][0]["evidence"][0]["excerpt"] = "By default the threshold\n is 1000\tpages."
    assessment["claims"][0]["units"][0]["quotes"][0]["quote"] = quote
    parsed = parse_review(json.dumps(assessment), basis)
    assert parsed["claims"][0]["units"][0]["quotes"][0]["quote"] == quote


@pytest.mark.parametrize("quote", ["threshold is 2000 pages", "threshold is ... pages", "Threshold is 1000 pages",
                                  "thresholdis1000pages", "threshold is 1000 words"])
def test_quote_matching_does_not_accept_changed_words_case_or_omitted_material(quote):
    basis, assessment = case()
    basis["claims"][0]["evidence"][0]["excerpt"] = "By default the threshold\n is 1000\tpages."
    assessment["claims"][0]["units"][0]["quotes"][0]["quote"] = quote
    with pytest.raises(ValueError, match="quote is not in its selected evidence"):
        parse_review(json.dumps(assessment), basis)


@pytest.mark.parametrize("mutation", [
    lambda value: value["requirements"].pop(),
    lambda value: value["requirements"].__setitem__(1, value["requirements"][0]),
    lambda value: value["requirements"][1].update(task_ref="r999"),
    lambda value: value["requirements"][1].update(kind="delivery"),
    lambda value: value["requirements"][1].update(question_refs=["q1"]),
    lambda value: value["requirements"][0].update(question_refs=[]),
    lambda value: value["investigations"].clear(),
    lambda value: value["investigations"].append(value["investigations"][0]),
    lambda value: value["investigations"][0].update(question_ref="q2"),
    lambda value: value["claims"][0]["units"].pop(),
    lambda value: value["claims"][0]["units"].append(value["claims"][0]["units"][0]),
    lambda value: value["claims"][0]["units"][0].update(quotes=[]),
    lambda value: value["claims"][0]["units"][0]["quotes"][0].update(quote="FULL waits for readers"),
    lambda value: value["claims"][0]["units"][0]["quotes"][0].update(evidence=2),
    lambda value: value["claims"][0]["units"][0]["quotes"][0].update(evidence=True),
    lambda value: value["paragraphs"][0]["units"].pop(),
    lambda value: value["paragraphs"][0]["units"][0].update(finding_units=[]),
    lambda value: value["paragraphs"][0]["units"][0].update(finding_units=[{"ref": "f2", "unit": 1}]),
    lambda value: value["paragraphs"][0]["units"][0].update(finding_units=[{"ref": "f1", "unit": 99}]),
    lambda value: value["paragraphs"][0]["units"][0].update(finding_units=[{"ref": "f1", "unit": 2}]),
])
def test_omission_reclassification_and_forged_support_are_rejected(mutation):
    basis, assessment = case()
    corrupt = copy.deepcopy(assessment)
    mutation(corrupt)
    with pytest.raises(ValueError):
        parse_review(json.dumps(corrupt), basis)


def test_report_comparator_swap_is_an_explicit_rejected_unit_even_with_valid_citation():
    basis, assessment = case()
    basis["claims"][0].update(units=text_units("A outperformed B."),
                              evidence=[{"excerpt": "A outperformed B."}])
    basis["paragraphs"][0]["units"] = text_units("A outperformed C.")
    assessment["claims"][0]["units"] = [{"unit": 1, "verdict": "supported",
        "quotes": [{"evidence": 1, "quote": "A outperformed B."}], "reason": "Exact comparator B."}]
    assessment["paragraphs"][0]["units"] = [{"unit": 1, "verdict": "unsupported", "basis": "findings",
        "finding_units": [{"ref": "f1", "unit": 1}], "reason": "The report substitutes C for B."}]
    issues = review_issues(parse_review(json.dumps(assessment), basis))
    assert len(issues) == 1 and issues[0]["kind"] == "report_scope"
    assert issues[0]["path"] == "summary" and issues[0]["unit"] == 1


@pytest.mark.parametrize("verdict", ["irrelevant", "uncertain"])
def test_bound_but_irrelevant_or_uncertain_investigation_blocks_acceptance(verdict):
    basis, assessment = case()
    assessment["investigations"][0].update(verdict=verdict, reason="This tool queried an unrelated subject.")
    issues = review_issues(parse_review(json.dumps(assessment), basis))
    issue = next(row for row in issues if row["kind"] == "investigation_relevance")
    assert issue["ref"] == "a1" and issue["question_ref"] == "q1"


def test_invalid_support_feedback_identifies_the_exact_paragraph_unit_and_available_findings():
    basis, assessment = case()
    assessment["paragraphs"][0]["units"][0]["finding_units"] = [{"ref": "f1", "unit": 99}]
    with pytest.raises(ValueError, match=r'summary unit 1:.*received.*99.*available finding units:.*f1.*1,2'):
        parse_review(json.dumps(assessment), basis)


def test_invalid_gap_feedback_lists_the_frozen_coverage_gaps():
    basis, assessment = case()
    basis["coverage"][1]["state"] = "evidence_found"
    assessment["paragraphs"][0]["units"][0].update(basis="gap", finding_units=[])
    with pytest.raises(ValueError, match=r'summary unit 1:.*available coverage gap paths: \[\].*unsupported or uncertain'):
        parse_review(json.dumps(assessment), basis)
