"""Exercise the experimental checks on production headless completion and real Chromium."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.types.json_value import JsonObject, decode_json_object
from tests.test_protocol_surface import TARGET
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_prerequisite_ab as experiment
from tools.completion_artifact_audit import artifacts, tool_receipts
from tools.completion_explanation_ab import Arm

pytestmark = pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")


def feedback_requests(directory: Path) -> list[str]:
    return [json.dumps(row["payload"]["messages"], ensure_ascii=False)
            for row in map(json.loads, (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines())
            if row.get("phase") == "request"]


@pytest.mark.parametrize("arm", ["A", "B"])
@pytest.mark.parametrize(("case", "next_step"), [
    ("missing_wait", "browser_wait"), ("wrong_wait", "browser_wait"), ("old_read", "browser_read"),
    ("other_tab", "browser_wait"), ("old_citation", "submit_research_answer"),
])
def test_research_wait_read_and_citation_use_the_same_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: Arm, case: str, next_step: str,
) -> None:
    replies: list[JsonObject] = []
    if next_step == "browser_wait":
        replies.append(experiment.code_action(experiment.WAIT + f"return {experiment.READ};"))
    elif next_step == "browser_read":
        replies.append(experiment.code_action(f"return {experiment.READ};"))
    replies.append(experiment.submission("e2", current=True))
    install_http(monkeypatch, replies)
    directory = tmp_path / "run"
    row = experiment.run_arm(directory, case, arm, TARGET)
    checks = row["checks"]
    assert isinstance(checks, list) and checks, row
    early = decode_json_object(checks[0])
    assert early["next_step"] == next_step, row
    if arm == "A" and case != "missing_wait":
        assert row["early_accepted"] and row["stop_reason"] == "done" and not row["passed"], row
        assert row["model_calls"] == 0
    else:
        assert not row["early_accepted"] and row["passed"], row
        assert row["model_calls"] == len(replies)
    receipts = tool_receipts(directory / "state")
    if case == "other_tab":
        waits = [r.result for r in receipts if r.result.call.name == "browser_wait" and r.result.ok]
        reads = [r.result for r in receipts if r.result.call.name == "browser_read" and r.result.ok]
        assert experiment.browser_document(waits[0]) != experiment.browser_document(reads[0])
    if arm == "B":
        assert early["accepted"] is False and str(early["feedback"]) in feedback_requests(directory)[0]
        proof = [data for _, body in artifacts(directory / "state")
                 if isinstance(data := body.get("data"), dict) and data.get("purpose") == "done"]
        assert any(experiment.CHECK in json.dumps(data) for data in proof)
        if case == "old_citation":
            assert sum(r.result.call.name == "browser_read" for r in receipts) == 2
            assert "[e2]" in str(early["feedback"]) and row["model_calls"] == 1


@pytest.mark.parametrize("arm", ["A", "B"])
def test_page_reset_checks_second_round_and_requires_the_specified_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: Arm,
) -> None:
    done: JsonObject = {"tool": "done", "args": {"summary": experiment.SUMMARY}}
    wrong_wait = experiment.ROUND.replace(experiment.WAIT, experiment.WAIT.replace("鱼丸", "福州"))
    install_http(monkeypatch, [experiment.code_action(wrong_wait), done,
                               experiment.code_action(experiment.WAIT + "return null;"), done,
                               experiment.code_action(f"return {experiment.READ};"), done])
    directory = tmp_path / "run"
    row = experiment.run_arm(directory, "page_reset", arm, TARGET, max_turns=9)
    assert row["stop_reason"] == "done", row
    checks = row["checks"]
    assert isinstance(checks, list)
    assert decode_json_object(checks[0])["next_step"] == "browser_navigate"
    if arm == "A":
        assert row["early_accepted"] and not row["passed"], row
        return
    assert [decode_json_object(check)["next_step"] for check in checks] == ["browser_navigate", "browser_wait", "browser_read", ""]
    assert row["passed"] and not row["early_accepted"], row
    assert row["model_calls"] == 6
    final = decode_json_object(checks[-1])
    evidence = final["evidence"]
    assert isinstance(evidence, list) and len(set(evidence)) == 12
    requests = feedback_requests(directory)
    assert "第 2 轮下一步是 browser_navigate" in requests[0]
    assert "第 2 轮下一步是 browser_wait" in requests[2]
    assert "第 2 轮下一步是 browser_read" in requests[4]
