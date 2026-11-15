"""Browser entry candidates through actual CodeMode and Chromium."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.codemode.runtime import NodeWorkerRuntime
from codey.codemode.types import ToolCatalogEntry
from codey.storage.input_attachments import InputAttachmentStore
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object, require_json_strings
from tests.test_browser_structured_upload_production import action as upload_action
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


def done(summary: str, *, explanation: bool = False) -> JsonObject:
    return {"tool": "done", "args": {"paragraphs": [summary, "已核对当前页面的记录。"]}
            if explanation else {"summary": summary}}


def upload_replies(directory: Path, *, explanation: bool) -> Iterator[JsonObject]:
    yield upload_action("observe")
    attachment, = InputAttachmentStore(directory / "state", "browser-file-fixture").root.glob("att_*.json")
    yield upload_action("select", target={"name": "Attachment"}, attachment_id=attachment.stem)
    yield upload_action("read", wait_for_text="UPLOAD_BYTES_82")
    yield done("已选择附件并读取页面，服务器接收状态尚未核验。", explanation=explanation)


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
    reason = parse_json_object(str(decode_json_object(result.value)["error"]))
    assert reason["code"] == ("target_ambiguous" if matches else "target_missing")
    assert reason["snapshot"] == {"page_id": "current", "generation": 12}
    candidates = reason["candidates"]
    assert isinstance(candidates, list) and len(candidates) == (matches or 1)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("scenario", ["basic", "stale_ref"])
def test_named_controls_use_fresh_snapshot_and_preserve_explicit_stale_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    code = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
            "const old=unwrap(await tools.browser_snapshot({})).elements.find(e=>e.name==='Load record').ref;"
            "unwrap(await browser.fill({target_name:'Locality',value:'福州'}));")
    if scenario == "stale_ref":
        code += "const stale=await tools.browser_click({ref:old}); if(stale.ok) throw Error('old ref was accepted');"
    code += ("unwrap(await browser.click({target_name:'Load record',role:'button'}));"
             "unwrap(await tools.browser_wait({condition:'text',value:'鱼丸',timeout_ms:5000}));"
             "return unwrap(await tools.browser_read({}));")
    install_http(monkeypatch, [action(code), done("净重 173 克，校验码 QUARTZ_851。")])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, scenario, "B", entry.TARGETS, TARGET, max_turns=5)
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    if scenario == "stale_ref":
        clicks = [r.result for r in receipts if r.result.call.name == "browser_click"]
        assert len(clicks) == 2 and not clicks[0].ok and clicks[1].ok
        assert row["failed_stale_refs"] == 1 and clicks[0].call.args["ref"] != clicks[1].call.args["ref"]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("scenario", ["structured", "legacy_structured"])
def test_direct_url_and_property_deliver_actual_value_on_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    code = browser_code().replace("return unwrap(await tools.browser_read({}));",
        "const body=unwrap(await tools.browser_read({})); const url=await browser.url({});"
        "const token=await browser.property({name:'__contractToken'}); return {body,url,token};")
    summary = "净重 173 克，校验码 QUARTZ_851。" if scenario == "structured" else (
        "净重 173 克，校验码 QUARTZ_851，token SCHEMA_TOKEN_74，URL " + URL)
    install_http(monkeypatch, [action(code), done(summary, explanation=scenario == "structured")])
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
@pytest.mark.parametrize("scenario", ["basic", "download"])
def test_result_projection_delivers_data_and_original_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str,
) -> None:
    if scenario == "basic":
        code = browser_code().replace("return unwrap(await tools.browser_read({}));", "return await browser.read({});")
        first, final = action(code), done("净重 173 克，校验码 QUARTZ_851。")
    else:
        code = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
                "const page=unwrap(await tools.browser_snapshot({}));")
        code += "const file=await browser.download({ref:page.elements.find(e=>e.name==='Download fixture').ref});"
        first = action(code + "return {file,body:await browser.read({})};")
        final = done("fixture-download.txt SHA256 " + hashlib.sha256(DOWNLOAD_BYTES).hexdigest())
    install_http(monkeypatch, [first, final])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, scenario, "B", entry.RESULTS, TARGET, max_turns=5)
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    read = next(r for r in receipts if r.result.ok and r.result.call.name == "browser_read")
    parent, = [r.result for r in receipts if r.result.ok and r.result.call.name == "codemode"
               and read.identity in require_json_strings(r.result.canonical["child_effect_ids"], "child_effect_ids")]
    value = decode_json_object(parent.canonical["value"])
    body = value if scenario == "basic" else decode_json_object(value["body"])
    assert body["status"] == "succeeded" and body["receipt_ref"] == read.identity
    assert body["data"] == {"text": read.result.model_text} and body["available_views"] == ["output"]
    if scenario != "basic":
        file = next(r for r in receipts if r.result.ok and r.result.call.name in {"browser_upload", "browser_download"})
        file_parent, = [r.result for r in receipts if r.result.ok and r.result.call.name == "codemode"
                        and file.identity in require_json_strings(r.result.canonical["child_effect_ids"], "child_effect_ids")]
        file_value = decode_json_object(file_parent.canonical["value"])
        projected = decode_json_object(file_value["file"])
        assert projected["receipt_ref"] == file.identity and projected["data"] == file.result.canonical["value"]
        assert projected["available_views"] == ["output", "value"]
    requests = [row["payload"] for row in map(parse_json_object, (directory / "provider.jsonl").read_text(
        encoding="utf-8").splitlines()) if row.get("phase") == "request"]
    assert read.identity in json.dumps(requests[-1], ensure_ascii=False)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize(("scenario", "config"), [("upload", entry.TARGETS), ("upload_bound", entry.RESULTS)])
def test_structured_upload_preserves_original_file_and_read_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, config: str,
) -> None:
    directory = tmp_path / "run"
    install_http(monkeypatch, upload_replies(directory, explanation=scenario == "upload_bound"))
    row = probe.run_arm(directory, scenario, "B", config, TARGET, max_turns=5)
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    file, = [r for r in receipts if r.result.call.name == "browser_upload"]
    read, = [r for r in receipts if r.result.call.name == "browser_read"]
    value = decode_json_object(file.result.canonical["value"])
    assert file.result.ok and read.result.ok and value["effect_verified"] is False
    assert value["sha256"] == hashlib.sha256(b"UPLOAD_BYTES_82\n").hexdigest()
    digest = value["sha256"]
    assert isinstance(digest, str) and digest in str(row["answer"]) and "UPLOAD_BYTES_82" in read.result.model_text
    for child in (file, read):
        parent, = [r.result for r in receipts if r.result.ok and r.result.call.name == "codemode"
                   and child.identity in require_json_strings(r.result.canonical["child_effect_ids"], "child_effect_ids")]
        assert parent.canonical["value"] == (child.result.canonical["value"] if child is file else child.result.model_text)


def test_named_upload_helper_projects_its_single_original_receipt(tmp_path: Path) -> None:
    calls: list[tuple[str, JsonObject]] = []
    value: JsonObject = {"name": "fixture.txt", "sha256": "hash", "effect_verified": False}

    def invoke(name: str, args: dict[str, object]) -> object:
        calls.append((name, decode_json_object(args)))
        if name == "browser_snapshot":
            return {"ok": True, "value": {"page_id": "p", "generation": 2,
                "elements": [{"name": "Attachment", "role": "textbox", "ref": "fresh"}]}}
        return {"ok": True, "value": value, "result_ref": "original-upload"}

    with entry.prototype(tmp_path, frozenset({entry.TARGETS, entry.RESULTS})):
        result = NodeWorkerRuntime().execute(
            "return await browser.upload({target_name:'Attachment',attachment_id:'selected'});",
            catalog=[ToolCatalogEntry(name, name) for name in ("browser_snapshot", "browser_upload")], invoke=invoke)
    assert result.ok
    assert calls == [("browser_snapshot", {}), ("browser_upload", {"ref": "fresh", "attachment_id": "selected"})]
    assert result.value == {"status": "succeeded", "data": value, "receipt_ref": "original-upload",
                            "available_views": ["output", "value"]}


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
    install_http(monkeypatch, [action(code), done("净重 173 克，校验码 QUARTZ_851。", explanation=True)])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, "structured", "B", entry.COMBINED, TARGET, max_turns=4, entry_features=entry.FEATURES)
    assert row["passed"], row
    receipts = tool_receipts(directory / "state")
    evaluations = [r.result for r in receipts if r.result.call.name == "browser_eval"]
    assert len(evaluations) == 1 and evaluations[0].call.args["code"] == "window.__contractToken"
