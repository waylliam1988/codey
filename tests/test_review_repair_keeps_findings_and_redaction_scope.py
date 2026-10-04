"""Repair cannot erase observed issues; redacted input stays partial."""
import json

from codey.reviews.core import parse_review_with_repair
from codey.reviews.input import prepare_review_input


def test_format_repair_cannot_erase_valid_finding():
    changes = {"ok": True, "files": [{"path": "app.py", "status": "M"}], "diff": ""}
    first = json.dumps({"verdict": "changes_requested", "findings": [
        {"path": "app.py", "issue": "wrong result"}, {"path": "outside.py", "issue": "invalid"}]})
    result = parse_review_with_repair(first, lambda _: '{"verdict":"approved","findings":[]}', changes=changes)
    assert [finding.issue for finding in result.findings] == ["wrong result"]
    assert not result.approved


def test_redacted_diff_cannot_be_full_scope():
    prepared = prepare_review_input(project="p", task="review", writer_summary="", changes={
        "ok": True, "files": [{"path": "app.py", "status": "M"}],
        "diff": '--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n+api_key = "sk-123456789012345678901234"',
    })
    assert "sk-123456789012345678901234" not in prepared.prompt
    assert not prepared.scope.is_complete


def test_private_key_body_is_not_sent_inside_code_diff():
    prepared = prepare_review_input(project="p", task="review", writer_summary="", changes={
        "ok": True, "files": [{"path": "app.py", "status": "M"}],
        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1,3 @@\n+-----BEGIN PRIVATE KEY-----\n+aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n+-----END PRIVATE KEY-----",
    })
    assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in prepared.prompt
    assert prepared.reviewer_view["diff"].count("\n") == 5
