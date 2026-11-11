"""Browser entry candidates through actual CodeMode and Chromium."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from codey.codemode.runtime import NodeWorkerRuntime
from codey.codemode.types import ToolCatalogEntry
from codey.storage.input_attachments import InputAttachmentStore
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_protocol_surface import TARGET, page_reset_action
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_entry_surface as entry
from tools import protocol_surface_ab as probe
from tools.browser_production_live_gate import DOWNLOAD_BYTES, URL
from tools.completion_artifact_audit import tool_receipts


def browser_code() -> str:
    return str(decode_json_object(page_reset_action()["args"])["code"])


def action(code: str, **inputs: str) -> JsonObject:
    return {"tool": "codemode", "args": {"code": code, "inputs": {"url": URL, **inputs}}}


def done(summary: str) -> JsonObject:
    return {"tool": "done", "args": {"summary": summary}}


@pytest.mark.parametrize("matches", [0, 2])
def test_target_resolution_returns_candidates_without_an_action(matches: int, tmp_path: Path) -> None:
    called: list[str] = []

    def invoke(name: str, args: dict[str, object]) -> object:
        called.append(name)
        return {"ok": True, "value": {"page_id": "current", "generation": 12,
            "elements": [{"name": "Go" if matches else "Other", "role": "button", "ref": str(i)}
                         for i in range(matches or 1)]}}

    with entry.prototype(tmp_path, frozenset({entry.TARGETS})):
        result = NodeWorkerRuntime().execute("return await browser.click({target_name:'Go',role:'button'});",
            catalog=[ToolCatalogEntry(name, name) for name in ("browser_snapshot", "browser_click")], invoke=invoke)
    assert result.ok and called == ["browser_snapshot"]
    reason = json.loads(str(decode_json_object(result.value)["error"]))
    assert reason["code"] == ("target_ambiguous" if matches else "target_missing")
    assert reason["snapshot"] == {"page_id": "current", "generation": 12}
    assert len(reason["candidates"]) == (matches or 1)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("scenario", ["basic", "stale_ref", "upload"])
def test_named_controls_use_fresh_snapshot_and_preserve_explicit_stale_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    if scenario == "upload":
        data = b"UPLOAD_BYTES_82\n"
        attachment = InputAttachmentStore(tmp_path / "inputs", "fixture").put("fixture.txt", base64.b64encode(data).decode())
        code = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
                "const upload=unwrap(await browser.upload({target_name:'Attachment',attachment_id:inputs.attachment}));"
                "unwrap(await tools.browser_wait({condition:'text',value:'UPLOAD_BYTES_82',timeout_ms:5000}));"
                "return {upload,body:unwrap(await tools.browser_read({}))};")
        first = action(code, attachment=str(attachment["id"]))
        final = done("fixture.txt UPLOAD_BYTES_82 SHA256 " + hashlib.sha256(data).hexdigest())
    else:
        code = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
                "const old=unwrap(await tools.browser_snapshot({})).elements.find(e=>e.name==='Load record').ref;"
                "unwrap(await browser.fill({target_name:'Locality',value:'福州'}));")
        if scenario == "stale_ref":
            code += "const stale=await tools.browser_click({ref:old}); if(stale.ok) throw Error('old ref was accepted');"
        code += ("unwrap(await browser.click({target_name:'Load record',role:'button'}));"
                 "unwrap(await tools.browser_wait({condition:'text',value:'鱼丸',timeout_ms:5000}));"
                 "return unwrap(await tools.browser_read({}));")
        first, final = action(code), done("净重 173 克，校验码 QUARTZ_851。")
    install_http(monkeypatch, [first, final])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, scenario, "B", entry.TARGETS, TARGET, max_turns=4)
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    if scenario == "stale_ref":
        clicks = [r.result for r in receipts if r.result.call.name == "browser_click"]
        assert len(clicks) == 2 and not clicks[0].ok and clicks[1].ok
        assert row["failed_stale_refs"] == 1 and clicks[0].call.args["ref"] != clicks[1].call.args["ref"]
    if scenario == "upload":
        upload, = [r.result for r in receipts if r.result.call.name == "browser_upload"]
        assert upload.ok and decode_json_object(upload.canonical["value"])["effect_verified"] is False


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("scenario", ["structured", "legacy_structured"])
def test_direct_url_and_property_deliver_actual_value_on_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    code = browser_code().replace("return unwrap(await tools.browser_read({}));",
        "const body=unwrap(await tools.browser_read({})); const url=await browser.url({});"
        "const token=await browser.property({name:'__contractToken'}); return {body,url,token};")
    install_http(monkeypatch, [action(code), done("净重 173 克，校验码 QUARTZ_851，token SCHEMA_TOKEN_74，URL " + URL)])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, scenario, "B", entry.PROPERTIES, TARGET, max_turns=4)
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    assert any(r.result.ok and r.result.call.name == "browser_eval"
               and r.result.call.args.get("code") == "window.__contractToken" for r in receipts)
    assert any(r.result.ok and r.result.call.name == "browser_tab_list" for r in receipts)
    parent = next(r.result for r in receipts if r.result.ok and r.result.call.name == "codemode")
    values = decode_json_object(parent.canonical["value"])
    assert decode_json_object(decode_json_object(values["url"])["value"]) == {"value": URL, "type": "string"}
    assert decode_json_object(decode_json_object(values["token"])["value"])["value"] == "SCHEMA_TOKEN_74"
    assert decode_json_object(decode_json_object(values["token"])["value"])["type"] == "string"


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("scenario", ["basic", "upload_bound", "download"])
def test_result_projection_delivers_data_and_original_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    if scenario == "basic":
        code = browser_code().replace("return unwrap(await tools.browser_read({}));", "return await browser.read({});")
        first, final = action(code), done("净重 173 克，校验码 QUARTZ_851。")
    else:
        code = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
                "const page=unwrap(await tools.browser_snapshot({}));")
        if scenario == "download":
            code += "const file=await browser.download({ref:page.elements.find(e=>e.name==='Download fixture').ref});"
            first = action(code + "return {file,body:await browser.read({})};")
            final = done("fixture-download.txt SHA256 " + hashlib.sha256(DOWNLOAD_BYTES).hexdigest())
        else:
            attachment = InputAttachmentStore(tmp_path / "inputs", "fixture").put(
                "fixture.txt", base64.b64encode(b"UPLOAD_BYTES_82\n").decode())
            code += ("const file=await browser.upload({ref:page.elements.find(e=>e.name==='Attachment').ref,"
                     "attachment_id:inputs.attachment});"
                     "unwrap(await tools.browser_wait({condition:'text',value:'UPLOAD_BYTES_82',timeout_ms:5000}));"
                     "return {file,body:await browser.read({})};")
            first, final = action(code, attachment=str(attachment["id"])), done("已选择附件。")
    install_http(monkeypatch, [first, final])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, scenario, "B", entry.RESULTS, TARGET, max_turns=4)
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    read = next(r for r in receipts if r.result.ok and r.result.call.name == "browser_read")
    parent = next(r.result for r in receipts if r.result.ok and r.result.call.name == "codemode")
    value = decode_json_object(parent.canonical["value"])
    body = value if scenario == "basic" else decode_json_object(value["body"])
    assert body["status"] == "succeeded" and body["receipt_ref"] == read.identity
    assert body["data"] == {"text": read.result.model_text} and body["available_views"] == ["output"]
    if scenario != "basic":
        file = next(r for r in receipts if r.result.ok and r.result.call.name in {"browser_upload", "browser_download"})
        projected = decode_json_object(value["file"])
        assert projected["receipt_ref"] == file.identity and projected["data"] == file.result.canonical["value"]
        assert projected["available_views"] == ["output", "value"]
    requests = [row["payload"] for row in map(json.loads, (directory / "provider.jsonl").read_text(
        encoding="utf-8").splitlines()) if row.get("phase") == "request"]
    assert read.identity in json.dumps(requests[1], ensure_ascii=False)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("scenario", ["basic", "snapshot_restore", "stale_ref"])
def test_wait_errors_are_prechecked_then_real_wait_can_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    bad = action("return await tools.browser_wait({condition:'text',value:'missing'});")
    invalid = action("return await tools.browser_wait({condition:'invalid',value:'missing',timeout_ms:5000});")
    code = browser_code()
    if scenario == "stale_ref":
        code = code.replace(
            "unwrap(await tools.browser_fill", "const old=s.elements.find(e=>e.name==='Load record').ref;unwrap(await tools.browser_fill", 1)
        code = code.replace("s=unwrap(await tools.browser_snapshot({}));unwrap(await tools.browser_click",
                            "await tools.browser_click({ref:old});s=unwrap(await tools.browser_snapshot({}));unwrap(await tools.browser_click", 1)
    code = code.replace("unwrap(await tools.browser_wait",
        "const timed=await tools.browser_wait({condition:'text',value:'NONEXISTENT_WAIT_MARKER',timeout_ms:100});"
        "if(timed.ok) throw Error('expected timeout');unwrap(await tools.browser_wait", 1)
    install_http(monkeypatch, [bad, invalid, action(code), done("净重 173 克，校验码 QUARTZ_851。")])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, scenario, "B", entry.WAIT, TARGET, max_turns=6)
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    failed = [r.result for r in receipts if r.result.call.name == "codemode" and not r.result.ok]
    assert len(failed) == 2 and "timeout_ms" in failed[0].model_text and "condition" in failed[1].model_text
    assert all(item.canonical["child_effect_ids"] == [] and "did not consume" not in item.model_text for item in failed)
    assert sum(r.result.ok and r.result.call.name == "browser_wait" for r in receipts) == 1
    timed, = [r.result for r in receipts if not r.result.ok and r.result.call.name == "browser_wait"]
    assert "wait timed out" in timed.model_text


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_failed_result_access_reports_file_action_without_a_second_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
            "const page=unwrap(await tools.browser_snapshot({}));"
            "const file=await tools.browser_download({ref:page.elements.find(e=>e.name==='Download fixture').ref});"
            "unwrap(await tools.browser_read({}));return file.path;")
    install_http(monkeypatch, [action(code), done("fixture-download.txt SHA256 " + hashlib.sha256(DOWNLOAD_BYTES).hexdigest())])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, "download", "B", entry.RESULTS, TARGET, max_turns=3)
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    download, = [r for r in receipts if r.result.ok and r.result.call.name == "browser_download"]
    failed, = [r.result for r in receipts if r.result.call.name == "codemode" and not r.result.ok]
    assert "Successful file actions already settled" in failed.model_text and download.identity in failed.model_text
    assert "repair" not in failed.canonical


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_navigation_and_tab_change_invalidate_old_refs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transitions = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
        "let old=unwrap(await tools.browser_snapshot({})).elements.find(e=>e.name==='Load record').ref;"
        "unwrap(await tools.browser_navigate({url:inputs.url}));"
        "if((await tools.browser_click({ref:old})).ok) throw Error('navigation kept old ref');"
        "old=unwrap(await tools.browser_snapshot({})).elements.find(e=>e.name==='Load record').ref;"
        "unwrap(await tools.browser_tab_new({url:inputs.url}));"
        "unwrap(await tools.browser_tab_switch({index:0}));"
        "if((await tools.browser_click({ref:old})).ok) throw Error('tab transition kept old ref');")
    install_http(monkeypatch, [action(transitions + browser_code()), done("净重 173 克，校验码 QUARTZ_851。")])
    row = probe.run_arm(tmp_path / "run", "stale_ref", "B", entry.TARGETS, TARGET, max_turns=4)
    assert row["passed"] and row["failed_stale_refs"] == 2, row


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_combined_entry_routes_through_original_browser_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code = ("await browser.navigate({url:inputs.url});"
            "await browser.fill({target_name:'Locality',value:'福州'});"
            "await browser.click({target_name:'Load record',role:'button'});"
            "await browser.wait({condition:'text',value:'鱼丸',timeout_ms:5000});"
            "const invalid=await browser.property({name:'bad;code()'});"
            "if(invalid.status!=='failed') throw Error('invalid identifier');"
            "return {body:await browser.read({}),url:await browser.url({}),"
            "token:await browser.property({name:'__contractToken'})};")
    install_http(monkeypatch, [action(code), done("净重 173 克，校验码 QUARTZ_851，token SCHEMA_TOKEN_74，URL " + URL)])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, "structured", "B", entry.COMBINED, TARGET, max_turns=4, entry_features=entry.FEATURES)
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    evaluations = [r.result for r in receipts if r.result.call.name == "browser_eval"]
    assert len(evaluations) == 1 and evaluations[0].call.args["code"] == "window.__contractToken"
