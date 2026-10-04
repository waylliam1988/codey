"""Bad or omitted review content cannot be promoted to complete approval."""
import json

import pytest

from codey.reviews.core import parse_review_response


@pytest.mark.parametrize("prefix", [
    '{"verdict":"approved","findings":{},"summary":"bad"}',
    '{"verdict":"approved","verdict":"changes_requested","findings":[]}',
])
def test_invalid_contract_before_valid_approval_is_not_hidden(prefix):
    result = parse_review_response(prefix + '\n{"verdict":"approved","findings":[]}')
    assert not result.approved
    assert result.status == "incomplete"


def test_nested_review_inside_metadata_is_not_an_independent_review():
    with pytest.raises(ValueError):
        parse_review_response('{"metadata":{"verdict":"approved","findings":[]}}')


def test_candidate_scan_exhaustion_prevents_approval():
    reply = '{"verdict":"approved","findings":[]}' + " {}" * 20
    assert not parse_review_response(reply).approved


def test_illegal_finding_among_valid_findings_marks_incomplete():
    reply = json.dumps({"verdict": "changes_requested", "findings": [
        {"path": "app.py", "issue": "Valid issue"}, {"path": "../other.py", "issue": "Invalid"},
    ]})
    result = parse_review_response(reply, changes={"ok": True, "files": [{"path": "app.py"}]})
    assert result.status == "incomplete"
    assert result.needs_writer_repair
    assert [finding.path for finding in result.findings] == ["app.py"]


def test_status_only_metadata_cannot_approve_review():
    with pytest.raises(ValueError):
        parse_review_response('{"status":"ok"}')


def test_status_only_metadata_does_not_conflict_with_real_review():
    result = parse_review_response('{"status":"ok"} {"verdict":"changes_requested","findings":[{"path":"app.py","issue":"bug"}]}')
    assert result.needs_writer_repair
