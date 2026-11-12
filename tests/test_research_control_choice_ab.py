"""Verify candidate selection, real click delivery, and cited browser readback."""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_receipt_bound_edit_surface_probe import install_http
from tests.test_research_click_readback_ab import answer
from tools import research_click_readback_ab as readback
from tools import research_control_choice_ab as probe
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target


def choose(choice: str) -> JsonObject:
    return {"tool": probe.TOOL, "args": {"choice": choice}}


def test_warehouse_status_accepts_matching_paraphrase() -> None:
    assert probe.answer_contains_fact("已经入库", "已入库")
    assert not probe.answer_contains_fact("尚未入库", "已入库")


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize(("scenario", "arm"), [
    ("visible", "D"), ("manual", "B"), ("interactive", "C"), ("wrong_record", "D"),
    ("timeout", "C"), ("stale", "C"), ("not_offered", "A"),
])
def test_direct_control_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, arm: str,
) -> None:
    case = probe.CASES["visible" if scenario == "visible" else "missing" if scenario == "wrong_record" else "interactive"]
    if scenario == "timeout":
        case = replace(case, html=case.html.replace("},850)", "},60000)"))
    browser: BrowserSession | None = None
    if scenario == "stale":
        original = probe.observed_controls

        def observe(active: BrowserSession, document: JsonObject) -> list[JsonObject]:
            nonlocal browser
            browser = active
            return original(active, document)

        monkeypatch.setattr(probe, "observed_controls", observe)

    def replies() -> Iterator[JsonObject]:
        if scenario == "visible":
            yield answer("LY-72 还有 6 本可借，19:40 闭馆。 [e1]")
            return
        if scenario == "stale":
            assert browser is not None and browser.navigate(case.url).ok
            yield choose("detail-1")
        if scenario == "wrong_record":
            yield choose("detail-2")
        yield choose("detail-1")
        if scenario == "manual":
            yield {"tool": "codemode", "args": {"code": "unwrap(await tools.browser_wait({condition:'text',value:'C-42',timeout_ms:5000}));"
                "return unwrap(await tools.browser_read({}));"}}
        if scenario in {"timeout", "not_offered"}:
            yield answer("目前仍显示实时记录尚未加载，状态和库位未确认。 [e1]", state="partial")
        else:
            yield answer(f"网页显示：{'，'.join(case.facts)}。 [e2]")

    install_http(monkeypatch, replies())
    directory = tmp_path / "run"
    row = probe.run_arm(directory, case, arm, Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10))
    assert row["passed"] is (scenario not in {"timeout", "not_offered"}), row
    assert row["successful_clicks"] == (0 if scenario in {"visible", "not_offered"} else 1)
    assert row["automatic_reads"] == (1 if scenario in {"interactive", "wrong_record", "stale"} else 0)
    if scenario in {"manual", "interactive"}:
        assert row["model_calls"] == (3 if scenario == "manual" else 2)
    requests = [record for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()
                if (record := decode_json_object(json.loads(line))).get("phase") == "request"]
    payload = decode_json_object(requests[0]["payload"])
    schema = json.dumps(payload["response_format"])
    prompt = json.dumps(payload["messages"], ensure_ascii=False)
    assert (probe.TOOL in schema) is (arm != "A")
    assert ("当前页面可直接选择的详情操作" in prompt) is (arm != "A")
    assert ("Examples for deciding the next action" in prompt) is (arm == "D")
    assert '"submit_research_answer"' in schema
    if arm != "A":
        offered = decode_json_object(json.loads((directory / "choices.jsonl").read_text(encoding="utf-8").splitlines()[0]))
        controls = offered["controls"]
        assert isinstance(controls, list) and controls
        assert all(str(decode_json_object(control)["ref"]) not in prompt for control in controls)
    if scenario == "wrong_record":
        assert "订单 A-173" in prompt and "订单 A-174" in prompt
    if scenario in {"wrong_record", "stale"}:
        selections = row["selections"]
        assert isinstance(selections, list)
        assert [decode_json_object(selection)["valid"] for selection in selections] == [False, True]
    if row["automatic_reads"]:
        receipt, = [item.result for item in tool_receipts(directory / "state") if item.result.audit.get("readback_of")]
        assert receipt.call.name == "browser_read" and receipt.audit["record_id"] == readback.record_id(case.question)
