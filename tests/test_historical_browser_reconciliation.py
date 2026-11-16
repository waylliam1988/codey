"""Unchanged live replies distinguish rejection from a successful model repair."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    parse_json_object,
    require_json_boolean,
    require_json_string,
    require_json_strings,
)
from tests.test_task_contract_fixed_replies import TARGET, install_original_http, saved_replies
from tools.browser_production_live_gate import run_case
from tools.completion_artifact_audit import tool_receipts

FIXTURE = Path(__file__).parent / "fixtures/historical_browser_failure_replies_20261115.json"
CASES = ("string-text-1111", "string-text-1112", "early-break-1114", "explanation-1115")
pytestmark = pytest.mark.usefixtures(
    "public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")


def historical_case(case: str) -> JsonObject:
    return decode_json_object(parse_json_object(FIXTURE.read_text(encoding="utf-8"))[case])


@pytest.mark.parametrize("case", CASES)
def test_unchanged_historical_replies_are_never_misreported_as_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    entry = historical_case(case)
    replies = require_json_strings(entry["replies"], "replies")
    assert [hashlib.sha256(reply.encode()).hexdigest() for reply in replies] == entry["reply_sha256"]
    install_original_http(monkeypatch, replies)
    row = run_case(tmp_path / "run", "chat", TARGET, max_turns=len(replies),
        explanation_output=require_json_boolean(entry["explanation_output"], "explanation_output"),
        stale_ref=require_json_boolean(entry["stale_ref"], "stale_ref"))
    assert "error" not in row, row
    assert row["passed"] is False and row["stop_reason"] != "done", row
    receipts = tool_receipts(tmp_path / "run/state")
    if case == "explanation-1115":
        assert decode_json_object(row["posthoc_output_checks"])["reason"] == "output_format_invalid"
        classification = "rejected_answer_requires_model_repair"
    else:
        classification = "tool_error_requires_model_correction"
        parents = [receipt.result for receipt in receipts if receipt.result.call.name == "codemode"]
        if case.startswith("string-text"):
            assert len(parents) == 4 and all(not result.ok for result in parents)
            assert sum(receipt.result.call.name == "browser_read" for receipt in receipts) == 4
        else:
            assert len(parents) == 4
            assert all("browser_click missing required arg 'ref'" in result.model_text for result in parents)
    record = {"case": case, "source": entry["source"], "source_sha256": entry["source_sha256"],
        "reply_sha256": entry["reply_sha256"], "reply_count": len(replies), "new_model_calls": 0,
        "classification": classification, "result": row}
    (tmp_path / "historical-reconciliation.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("case", ["omitted-facts-1113", "explanation-1115"])
def test_historical_bad_completion_needs_only_an_answer_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    replies = (saved_replies("structured") if case == "omitted-facts-1113" else
               require_json_strings(historical_case(case)["replies"], "replies"))
    corrected = "已读取当前页面记录。\n\n净重173克，校验码QUARTZ_851。"
    install_original_http(monkeypatch, [*replies, json.dumps({"tool": "done", "args": {
        "paragraphs": corrected.split("\n\n")}})])
    row = run_case(tmp_path / "run", "chat", TARGET, max_turns=len(replies) + 1,
        explanation_output=True, restore_snapshots=True)
    assert row["passed"] is True and row["stop_reason"] == "done", row
    assert require_json_string(parse_json_object(str(row["answer"]))["explanation"], "explanation") == corrected
    repairs = row["repair_attempts"]
    assert isinstance(repairs, list)
    assert any("done missing required arg 'paragraphs'" in str(decode_json_object(repair)["reason"])
               for repair in repairs), row
    for name in ("browser_click", "browser_read"):
        selected = [receipt.result for receipt in tool_receipts(tmp_path / "run/state") if receipt.result.call.name == name]
        assert len(selected) == 1 and selected[0].ok, name
