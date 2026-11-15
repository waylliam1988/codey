"""Answer-only repairs exercise the real entry, browser, receipts and restoration."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from codey.codemode.json_edit import JSON_FIELD_EDIT_PROGRAM
from codey.storage.input_attachments import InputAttachmentStore
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object
from tests.test_browser_structured_upload_production import action as upload_action
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_production_live_gate import HTML, SOURCE_SUCCESS_MARKER, TWO_PARAGRAPHS, URL, run_case
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import ACQUISITION, TOKEN, Target

TARGET = Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize(("amount", "code"), [(173, "QUARTZ_851"), (284, "TOPAZ_029")])
def test_fact_and_paragraph_repairs_use_current_receipts_without_repeating_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, amount: int, code: str,
) -> None:
    stale = ("已读取页面记录。\n\n上一次结果是 1173 克，校验码 OLD_000。" if amount == 173 else
             "已读取页面记录。\n\n上一次结果是 173 克，校验码 QUARTZ_851。")
    escaped = f"观察过程。\\n\\n净重为 {amount} 克，校验码 {code}。"
    fractional = f"已读取当前记录。\n\n净重为 {amount}.5 克，校验码 {code}。"
    repeated = f"已读取 {URL}，token为 {TOKEN}。\n\n净重为 {amount} 克，校验码 {code}。"
    three_paragraphs = f"已读取当前记录。\n\n净重为 {amount} 克。\n\n校验码 {code}。"
    corrected = f"已读取当前记录，路径示例 `C:\\new`。\n\n净重为 {amount} 克，校验码 {code}，代码示例 `\\n`。"
    replies: list[JsonObject] = [{"tool": "done", "args": {"summary": draft}}
                                for draft in (stale, fractional, escaped, repeated, three_paragraphs, corrected)]
    install_http(monkeypatch, replies)
    acquisition: JsonObject = {"tool": "codemode", "args": {
        "code": ACQUISITION,
        "inputs": {"url": URL, "city": "福州", "wait_text": "鱼丸"}}}
    directory = tmp_path / "run"
    row = run_case(directory, "chat", TARGET, max_turns=7, explanation_output=True, restore_snapshots=True,
        acquisition_action=acquisition,
        fixture_html=HTML.replace("amount:173,code:'QUARTZ_851'", f"amount:{amount},code:'{code}'"),
        expected_amount=amount, expected_code=code)
    assert row["passed"] is True and row["stop_reason"] == "done", row
    assert parse_json_object(str(row["answer"]))["explanation"] == corrected
    attempts = row["repair_attempts"]
    assert isinstance(attempts, list) and len(attempts) == 5
    first, numeric, second, third, fourth = [decode_json_object(item) for item in attempts]
    assert "answer_text_missing" in str(first["reason"])
    assert str(amount) in str(first["reason"]) and stale in str(first["reason"])
    assert "answer_text_missing" in str(numeric["reason"]) and fractional in str(numeric["reason"])
    assert "output_format_invalid" in str(second["reason"]) and escaped in str(second["reason"])
    assert "answer_text_forbidden" in str(third["reason"]) and repeated in str(third["reason"])
    assert "output_format_invalid" in str(fourth["reason"])
    checks = decode_json_object(row["posthoc_output_checks"])
    assert checks["facts_mentioned"] is True and checks["schema_valid"] is True
    receipts = tool_receipts(directory / "state")
    assert sum(item.result.call.name == "browser_read" for item in receipts) == 1
    assert sum(item.result.call.name == "browser_click" for item in receipts) == 1
    assert not any(item.result.call.name == "browser_upload" for item in receipts)
    assert row["restored_snapshots"] and row["raw_explanation_preserved"] is True
    requests = [parse_json_object(line) for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
    last = next(item for item in reversed(requests) if item.get("phase") == "request")
    messages = decode_json_object(last["payload"])["messages"]
    assert isinstance(messages, list)
    assert any(isinstance(message, dict) and isinstance(content := message.get("content"), str)
               and escaped in content for message in messages)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_missing_one_fact_repairs_only_explanation_after_committed_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = "已读取并写入当前记录。\n\n净重为173克，其他字段已保留。"
    corrected = draft + "校验码为 QUARTZ_851。"
    install_http(monkeypatch, [{"tool": "done", "args": {"summary": text}} for text in (draft, corrected)])
    program = ACQUISITION.replace("const tabs=",
        "await (async () => {\n" + JSON_FIELD_EDIT_PROGRAM + "\n})();\nconst tabs=")
    acquisition: JsonObject = {"tool": "codemode", "args": {"code": program, "inputs": {
        "url": URL, "city": "福州", "wait_text": "鱼丸", "path": "result.json",
        "changes": {"amount": 173, "code": "QUARTZ_851"}}}}
    directory = tmp_path / "run"
    row = run_case(directory, "hybrid", TARGET, max_turns=3, explanation_output=True, restore_snapshots=True,
        acquisition_action=acquisition)
    assert row["passed"] is True and row["stop_reason"] == "done", row
    assert parse_json_object(str(row["answer"]))["explanation"] == corrected
    attempts = row["repair_attempts"]
    assert isinstance(attempts, list) and len(attempts) == 1
    feedback = str(decode_json_object(attempts[0])["reason"])
    assert "answer_text_missing" in feedback and 'Expected selected value (JSON): "QUARTZ_851"' in feedback
    assert 'Expected selected value (JSON): "173"' not in feedback
    assert "Do not repeat the tool execution" in feedback
    receipts = tool_receipts(directory / "state")
    for tool in ("browser_read", "browser_click", "read_file", "edit"):
        selected = [receipt for receipt in receipts if receipt.result.call.name == tool]
        assert len(selected) == 1 and selected[0].result.ok, tool
    read = next(receipt for receipt in receipts if receipt.result.call.name == "browser_read")
    assert read.identity in feedback
    assert row["restored_snapshots"] and row["raw_explanation_preserved"] is True


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("layout", ["forbidden_search", "specified_target"])
def test_first_request_prefetch_does_not_consume_background_or_revoked_addresses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str,
) -> None:
    other = "https://example.com/forbidden-background"
    task = (f"Do not open <{other}>. Search the web for alternatives to <{other}>. Open <{URL}>."
            if layout == "forbidden_search" else f"Open only <{URL}>.\n\nSource: <{other}>")
    install_http(monkeypatch, [{"tool": "done", "args": {"summary": SOURCE_SUCCESS_MARKER}}])
    directory = tmp_path / "run"
    row = run_case(directory, "chat", TARGET, max_turns=1, source_failure=True, source_attempt_only=True,
                   source_status=200, task_transform=lambda _: task)
    assert row["passed"] is True, row
    sources = [item for item in tool_receipts(directory / "state") if item.result.call.name == "open_url"]
    assert len(sources) == 1 and sources[0].result.call.args["url"] == URL
    assert sources[0].result.call.call_id == "required-source:0"
    transcript = [parse_json_object(line) for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
    first = next(item for item in transcript if item.get("phase") == "request")
    assert sources[0].identity in json.dumps(first["payload"], ensure_ascii=False)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("format_retry", [False, True])
def test_upload_uncertainty_and_format_repair_never_repeat_attachment_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, format_retry: bool,
) -> None:
    attachment = InputAttachmentStore(tmp_path / "inputs", "explanation-repair").put(
        "fixture.txt", base64.b64encode(b"UPLOAD_BYTES_82\n").decode("ascii"))
    corrected = ("I cannot confirm whether the server has accepted the attachment. Selected fixture.txt.\n\n"
                 "The page contains UPLOAD_BYTES_82; server acceptance is unverified.")
    drafts = ([corrected.replace("\n", r"\n"), corrected] if format_retry else [corrected])
    replies: list[JsonObject] = [upload_action("observe"),
        upload_action("select", attachment_id=str(attachment["id"]), target={"name": "Attachment"}), upload_action("read")]
    replies.extend({"tool": "done", "args": {"summary": draft}} for draft in drafts)
    install_http(monkeypatch, replies)
    properties: JsonObject = {key: {"type": "string"} for key in ("explanation", "filename", "sha256", "page_text")}
    properties["explanation"] = {"type": "string", "pattern": TWO_PARAGRAPHS}
    output_schema: JsonObject = {"type": "object", "properties": properties,
        "required": list(properties), "additionalProperties": False}
    directory = tmp_path / "run"
    row = run_case(directory, "chat", TARGET, assembled_upload=True, restore_snapshots=True,
        upload_file=True, max_turns=len(replies), explanation_schema=output_schema)
    assert row["passed"] is True and row["stop_reason"] == "done", row
    assert parse_json_object(str(row["answer"]))["explanation"] == corrected
    attempts = row["repair_attempts"]
    assert isinstance(attempts, list) and len(attempts) == int(format_retry)
    if format_retry:
        assert "output_format_invalid" in str(decode_json_object(attempts[0])["reason"])
    receipts = tool_receipts(directory / "state")
    assert sum(item.result.call.name == "browser_upload" for item in receipts) == 1
    assert sum(item.result.call.name == "browser_read" for item in receipts) == 1
