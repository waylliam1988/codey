"""Current production controls for browser entry experiments with real Chromium."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from codey.codemode.replay import DurableCodeModeReplay
from codey.runtime.core.models import ToolCall
from codey.storage.input_attachments import InputAttachmentStore
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_browser_entry_candidates import action, browser_code, done
from tests.test_protocol_surface import TARGET
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_entry_surface as entry
from tools import browser_production_live_gate as gate
from tools import protocol_surface_ab as probe
from tools.browser_entry_followup_audit import RESULT_DATA, request_results
from tools.completion_artifact_audit import tool_receipts

pytestmark = pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")


def first_feedback(directory: Path) -> str:
    return next(text for results in request_results(directory).values() for text in results)


@pytest.mark.parametrize("scenario", ["basic", "snapshot_restore", "stale_ref"])
@pytest.mark.parametrize("arm", ["A", "B"])
def test_identical_wait_fault_then_real_timeout_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, arm: str,
) -> None:
    registered_invalid: list[ToolCall] = []
    register_child = DurableCodeModeReplay.child

    def record_child(replay: DurableCodeModeReplay, call: ToolCall, effect_id: str) -> None:
        if call.name == "browser_wait" and ("timeout_ms" not in call.args or call.args.get("condition") == "invalid"):
            registered_invalid.append(call)
        register_child(replay, call, effect_id)

    monkeypatch.setattr(DurableCodeModeReplay, "child", record_child)
    recovery = action("const timed=await tools.browser_wait({condition:'text',value:'NONEXISTENT_MARKER',timeout_ms:100});"
                      "if(timed.ok) throw Error('expected timeout');"
                      "unwrap(await tools.browser_wait({condition:'text',value:'鱼丸',timeout_ms:5000}));"
                      "return unwrap(await tools.browser_read({}));")
    install_http(monkeypatch, [recovery, done("净重 173 克，校验码 QUARTZ_851。")])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, scenario, arm, entry.WAIT_RECOVERY, TARGET, max_turns=4)
    assert row["passed"] and row["model_calls"] == 2, row
    receipts = [r.result for r in tool_receipts(directory / "state")]
    fault, recovered = [r for r in receipts if r.call.name == "codemode"]
    scripted = decode_json_object(entry.diagnostic_action(entry.WAIT_RECOVERY, scenario, arm, gate.URL)["args"])
    assert all(fault.call.args[key] == value for key, value in scripted.items())
    assert not fault.ok and recovered.ok
    assert not registered_invalid
    error = first_feedback(directory).splitlines()[1]
    field = "condition" if scenario == "snapshot_restore" else "timeout_ms"
    assert error.startswith("browser_wait") and f"'{field}'" in error
    timed, waited = [r for r in receipts if r.call.name == "browser_wait"]
    assert not timed.ok and "wait timed out" in timed.model_text and waited.ok
    if scenario == "stale_ref":
        stale, fresh = [r for r in receipts if r.call.name == "browser_click"]
        assert decode_json_object(stale.canonical["value"])["reason"] == "stale_ref"
        assert fresh.ok and fresh.call.args["ref"] != stale.call.args["ref"]
    if scenario == "snapshot_restore":
        assert (directory / "snapshots.jsonl").stat().st_size > 0


@pytest.mark.parametrize("scenario", ["structured", "legacy_structured"])
@pytest.mark.parametrize("mode", ["missing_token", "missing_business_facts", "complete"])
def test_properties_distinguish_missing_read_from_missing_final_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, mode: str,
) -> None:
    directory = tmp_path / "run"
    summary = "已读取页面。" if mode == "missing_business_facts" else "净重 173 克，校验码 QUARTZ_851。"
    if scenario == "legacy_structured":
        summary += f" URL {gate.URL}，token SCHEMA_TOKEN_74。"
    replies: list[JsonObject] = [done(summary)] * 3
    config = entry.PROPERTIES_DELIVERY
    if mode == "missing_token":
        # Omit only the property read. A fabricated correct-looking token in done cannot supply its receipt.
        code = browser_code().replace("return unwrap(await tools.browser_read({}));",
            "const body=unwrap(await tools.browser_read({}));const url=unwrap(await browser.url({})).value;return {body,url};")
        replies.insert(0, action(code))
        config = entry.PROPERTIES
    install_http(monkeypatch, replies)
    row = probe.run_arm(directory, scenario, "B", config, TARGET, max_turns=4)
    assert row["passed"] is (mode == "complete"), row
    receipts = [r.result for r in tool_receipts(directory / "state") if r.result.ok]
    tabs, = [r for r in receipts if r.call.name == "browser_tab_list"]
    assert decode_json_object(tabs.canonical["value"])["page_url"] == gate.URL
    read, = [r for r in receipts if r.call.name == "browser_read"]
    assert "173" in read.model_text and "QUARTZ_851" in read.model_text
    first_result = first_feedback(directory)
    assert all(fact in first_result for fact in (gate.URL, "173", "QUARTZ_851"))
    evaluations = [r for r in receipts if r.call.name == "browser_eval"]
    if mode == "missing_token":
        assert not evaluations and "SCHEMA_TOKEN_74" not in first_result
        assert row["stop_reason"] != "done"
    else:
        token, = evaluations
        assert token.call.args["code"] == "window.__contractToken"
        assert decode_json_object(token.canonical["value"])["value"] == "SCHEMA_TOKEN_74"
        assert "SCHEMA_TOKEN_74" in first_result
        assert row["stop_reason"] == "done"


@pytest.mark.parametrize("scenario", ["upload_bound", "download"])
def test_real_ambiguous_controls_and_named_file_actions_with_original_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    monkeypatch.setattr(gate, "HTML", gate.HTML.replace("</body>",
        "<button>Duplicate</button><button>Duplicate</button></body>"))
    code = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
            "const ambiguous=await browser.click({target_name:'Duplicate',role:'button'});"
            "const absent=await browser.click({target_name:'No such control',role:'button'});")
    inputs: dict[str, str] = {}
    if scenario == "upload_bound":
        content = b"UPLOAD_BYTES_82\n"
        attachment = InputAttachmentStore(tmp_path / "inputs", "fixture").put("fixture.txt", base64.b64encode(content).decode())
        inputs["attachment"] = str(attachment["id"])
        code += ("const file=await browser.upload({target_name:'Attachment',attachment_id:inputs.attachment});"
                 "unwrap(await tools.browser_wait({condition:'text',value:'UPLOAD_BYTES_82',timeout_ms:5000}));")
        summary = "已选择附件。"
    else:
        content = gate.DOWNLOAD_BYTES
        code += "const file=await browser.download({target_name:'Download fixture',role:'link'});"
        summary = "fixture-download.txt SHA256 " + hashlib.sha256(content).hexdigest()
    install_http(monkeypatch, [action(code + "return {ambiguous,absent,file,body:await browser.read({})};", **inputs), done(summary)])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, scenario, "B", entry.COMBINED, TARGET, max_turns=3,
                        entry_features=frozenset({entry.TARGETS, entry.RESULTS}))
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    assert not any(r.result.call.name == "browser_click" for r in receipts)
    parent, = [r.result for r in receipts if r.result.call.name == "codemode"]
    data = decode_json_object(parent.canonical["value"])
    snapshots = [decode_json_object(r.result.canonical["value"]) for r in receipts
                 if r.result.call.name == "browser_snapshot" and r.result.ok]
    assert len(snapshots) == 3
    for index, (key, error) in enumerate((("ambiguous", "target_ambiguous"), ("absent", "target_missing"))):
        failure = decode_json_object(data[key])
        assert failure["status"] == "failed"
        detail = decode_json_object(json.loads(str(failure["error"])))
        assert detail["code"] == error
        assert detail["snapshot"] == failure["snapshot"] == {key: snapshots[index][key] for key in ("page_id", "generation")}
        candidates = detail["candidates"]
        assert isinstance(candidates, list)
        assert candidates.count({"name": "Duplicate", "role": "button"}) == 2
    file, = [r for r in receipts if r.result.call.name in {"browser_upload", "browser_download"}]
    value = decode_json_object(file.result.canonical["value"])
    assert value["sha256"] == hashlib.sha256(content).hexdigest()
    if scenario == "upload_bound":
        assert file.result.call.args["attachment_id"] == inputs["attachment"]
        assert value["effect_verified"] is False
    projected = decode_json_object(data["file"])
    assert projected["data"] == value and projected["receipt_ref"] == file.identity
    feedback = first_feedback(directory)
    assert feedback.startswith(RESULT_DATA)
    wire = decode_json_object(json.loads(feedback.removeprefix(RESULT_DATA)))
    assert decode_json_object(wire["value"])["file"] == projected
