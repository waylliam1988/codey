"""Scripted publication on both arms over the same settled Chromium receipts."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from codey.types.json_value import JsonObject, decode_json_object
from tests.test_protocol_surface import TARGET
from tools import receipt_field_assembly_ab as experiment
from tools.completion_artifact_audit import tool_receipts


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_shared_real_receipts_detect_copy_errors_and_assemble_exact_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "acquisition"
    evidence = experiment.acquire(directory, TARGET, 2)
    receipts = tool_receipts(directory / "state")
    assert evidence == experiment.shared_evidence(receipts)
    expected = experiment.fields(evidence)
    explanation = "在福州页面填写地点并点击按钮，加载了样本记录。"
    correct: JsonObject = {"explanation": explanation, **expected}
    wrong: JsonObject = {**correct, "page_url": str(expected["page_url"]).replace("browser-record", "browser_record")}
    missing = {key: value for key, value in correct.items() if key != "token"}
    replies = iter([wrong, missing, correct, {"explanation": explanation}])
    bundles: list[object] = []

    def scripted(url: str, payload: dict[str, object], headers: dict[str, str], **kwargs: object) -> dict[str, object]:
        observe = kwargs["observe"]
        assert callable(observe)
        observe(attempt=1, data=json.dumps(payload).encode(), phase="request", response_bytes=0, seconds=0.0)
        messages = payload["messages"]
        assert isinstance(messages, list)
        bundles.append(decode_json_object(messages[1])["content"])
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(next(replies))}}]}

    def no_service_state(_url: str) -> JsonObject:
        return {}

    monkeypatch.setattr("codey.providers.api_generation.generate", scripted)
    monkeypatch.setattr("tools.completion_explanation_ab.service_state", no_service_state)
    a = experiment.run_arm(tmp_path / "A", TARGET, "A", evidence, 3)
    b = experiment.run_arm(tmp_path / "B", TARGET, "B", evidence, 3)
    assert a["answer"] == b["answer"] == correct
    assert a["passed"] and b["passed"] and not a["provider_error"] and not b["provider_error"]
    assert a["evidence_sha256"] == b["evidence_sha256"] and len(bundles) == 4
    assert bundles == [json.dumps(evidence, ensure_ascii=False, sort_keys=True)] * 4
    assert a["model_calls"] == 3 and b["model_calls"] == 1
    attempts = a["attempts"]
    assert isinstance(attempts, list)
    assert decode_json_object(decode_json_object(attempts[0])["fields"])["page_url"] == "incorrect"
    assert decode_json_object(decode_json_object(attempts[1])["fields"])["token"] == "missing"
    assert experiment.aggregate([a])["first_fields"] == {"total": 5, "incorrect": 1, "missing": 0}
    assert experiment.aggregate([b])["final_fields"] == {"total": 5, "incorrect": 0, "missing": 0}

    record = next(r for r in receipts if r.result.call.args.get("code") == experiment.RECORD)
    incomplete = replace(record.result, truncated=True)
    with pytest.raises(ValueError):
        experiment.shared_evidence([replace(r, result=incomplete) if r is record else r for r in receipts])


def test_type_mismatch_and_unauthorized_fields_are_rejected() -> None:
    expected: JsonObject = {"page_url": "https://example.com/path", "token": "token", "amount": 1, "unit": "克", "code": "ABC"}
    text = "福州页面加载了样本记录。"
    _, a_issues = experiment.assemble(json.dumps({**expected, "amount": True, "explanation": text}), "A", expected)
    assert any("amount" in issue for issue in a_issues)
    b, b_issues = experiment.assemble(json.dumps({"explanation": text, "token": "fabricated"}), "B", expected)
    assert b_issues and b["token"] == expected["token"]
    _, duplicate = experiment.assemble('{"explanation":"福州","explanation":"福州"}', "B", expected)
    assert duplicate
