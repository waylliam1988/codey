"""Contracts for D; no test assumes a revision is beneficial."""

import json

import pytest

from tests.manual import research_semantic_review_d as d


def test_frozen_cases_are_natural_reports_with_unchanged_drafts():
    cases = d.load_cases()
    assert len(cases) == 6
    assert all(case["draft"] and case["draft_digest"] if "draft_digest" in case else case["draft"] for case in cases)
    assert all(case["source_report"].endswith("research-report.txt") for case in cases)
    assert all(len(case["evidence"]) >= 1 for case in cases)
    assert len({d.digest(case["draft"]) for case in cases}) == len(cases)


def test_control_has_no_revision_calls_and_other_arms_have_two():
    assert d.ARMS == ("control", "self_check", "b_feedback")
    assert all("review" in d.REVIEWS[arm] for arm in ("self_check", "b_feedback"))
    assert d.REVIEWS["self_check"] != d.REVIEWS["b_feedback"]


def test_review_packet_contains_same_frozen_answer_and_evidence():
    case = d.load_cases()[0]
    packet = d.review_packet(case)
    assert packet["draft"] == case["draft"]
    assert packet["evidence"] == case["evidence"]
    assert all("gold" not in unit for unit in packet["units"])


def test_revision_packet_treats_review_as_untrusted_suggestion():
    case = d.load_cases()[0]
    packet = d.revision_messages(case, {"parsed": {"u1": {"verdict": "contradicted"}}})
    body = json.loads(packet[1]["content"])
    assert body["draft"] == case["draft"]
    assert body["evidence"] == case["evidence"]
    assert body["review"]["parsed"]["u1"]["verdict"] == "contradicted"
    assert "untrusted suggestion" in packet[0]["content"]


def test_score_reports_coverage_forbidden_claims_and_citations():
    case = next(row for row in d.load_cases() if row["id"] == "condition_scope")
    scored = d.score(case, "R4 21 天且 audit，R5 28 天或 signature。[e1][e2]")
    assert scored["required_hit"] == 4
    assert scored["forbidden_count"] == 0
    assert scored["citation_labels_valid"] is True


def test_score_detects_invalid_citation_and_forbidden_claim():
    case = next(row for row in d.load_cases() if row["id"] == "known_url")
    scored = d.score(case, "3.22.0，key alone suffices。[e9]")
    assert scored["forbidden_count"] == 1
    assert scored["citation_labels_valid"] is False


def test_freeze_rejects_changed_production_or_frozen_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "production_identity", lambda: {"codey/a.py": "one"})
    d.freeze(tmp_path)
    monkeypatch.setattr(d, "production_identity", lambda: {"codey/a.py": "two"})
    with pytest.raises(RuntimeError, match="frozen D inputs"):
        d.freeze(tmp_path)


def test_extract_markdown_only_removes_outer_fence():
    raw = {"choices": [{"message": {"content": "```markdown\n# answer\n```"}}]}
    assert d.extract_markdown(raw) == "# answer"


def test_control_score_is_reproducible():
    case = d.load_cases()[0]
    assert d.score(case, case["draft"]) == d.score(case, case["draft"])
