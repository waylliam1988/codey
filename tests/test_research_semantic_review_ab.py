"""Keep the experiment from grading a paraphrase or leaking its answer key."""

import json

import pytest

from tests.manual import research_semantic_review_ab as ab
from tests.manual.research_semantic_review_cases import cases


def test_exact_unicode_spans_and_no_gold_leak():
    for case in cases():
        data = ab.model_input(case)
        assert "gold" not in data
        assert "split" not in data
        assert len(data["units"]) == len(case["gold"])
        for unit in data["units"]:
            assert case["markdown"][unit["start"]:unit["end"]] == unit["text"]
        for source in data["evidence"]:
            assert source["locator"]["end"] == len(source["excerpt"])


@pytest.mark.parametrize("reviews", [
    [{"id": "u1", "verdict": "entailed", "reason": "ok"}] * 2,
    [{"id": "u1", "verdict": "entailed", "reason": "ok"}],
    [{"id": "u1", "verdict": "evidence_backed", "reason": "ok"},
     {"id": "u2", "verdict": "entailed", "reason": "ok"}],
])
def test_protocol_rejects_missing_duplicate_and_self_certified_status(reviews):
    with pytest.raises(ValueError):
        ab.parse_reply(json.dumps({"reviews": reviews}), [{"id": "u1"}, {"id": "u2"}])


def test_invalid_output_is_not_counted_as_a_detection():
    graded = ab.grade(cases()[0], {})
    assert all(row["predicted"] == "invalid" for row in graded)
    assert not any(row["predicted"] in ab.BAD for row in graded)


def test_freeze_rejects_changed_prompts(tmp_path, monkeypatch):
    ab.freeze(tmp_path)
    ab.freeze(tmp_path)
    monkeypatch.setitem(ab.PROMPTS, "focused", "changed after preregistration")
    with pytest.raises(RuntimeError, match="frozen experiment changed"):
        ab.freeze(tmp_path)


def test_dataset_has_positive_controls_and_uncited_assertions_in_both_splits():
    for split in ("dev", "holdout"):
        selected = [case for case in cases() if case["split"] == split]
        assert len(selected) == 8
        assert all("entailed" in case["gold"] for case in selected)
        assert any("not_claim" in case["gold"] for case in selected)
        assert any(not unit["evidence_refs"] and gold in ab.BAD for case in selected
                   for unit, gold in zip(ab.units(case["markdown"]), case["gold"], strict=True))
