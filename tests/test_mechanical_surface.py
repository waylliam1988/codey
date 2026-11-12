"""Deterministic counterexamples through real production receipts and Chromium."""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from codey.codemode import catalog
from codey.codemode.runtime import NodeWorkerRuntime
from codey.codemode.types import ToolCatalogEntry
from codey.policies.tool_requirement import ToolSuccessRequirement, argument_digest
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolCall, ToolResult
from codey.storage.input_attachments import InputAttachmentStore
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import mechanical_surface as mechanical
from tools import receipt_bound_edit_surface_probe as probe
from tools.browser_production_live_gate import URL
from tools.completion_artifact_audit import EffectReceipt, tool_receipts
from tools.completion_explanation_ab import Target


def test_typed_read_and_named_helpers_forward_real_leaves(tmp_path: Path) -> None:
    calls: list[str] = []

    def invoke(name: str, args: dict[str, object]) -> object:
        calls.append(name)
        values: dict[str, object] = {
            "browser_read": "actual body",
            "browser_snapshot": {"elements": [{"name": "Locality", "ref": "current"}]},
            "browser_fill": "filled",
        }
        if name == "browser_fill":
            assert args == {"ref": "current", "value": "福州"}
        return {"ok": True, "value": values[name]}

    with mechanical.prototype(tmp_path, "basic", enabled=True):
        result = NodeWorkerRuntime().execute(
            "unwrap(await tools.browser_fill_named({name:'Locality',value:'福州'})); "
            "return unwrap(await tools.browser_read({})).text;",
            catalog=[ToolCatalogEntry(name, name) for name in ("browser_snapshot", "browser_fill", "browser_read")],
            invoke=invoke,
        )
    assert result.ok and result.value == "actual body", result
    assert calls == ["browser_snapshot", "browser_fill", "browser_read"]


@pytest.mark.parametrize("matches", [0, 2])
def test_named_action_rejects_missing_or_ambiguous_target_without_effect(tmp_path: Path, matches: int) -> None:
    calls: list[str] = []

    def invoke(name: str, args: dict[str, object]) -> object:
        calls.append(name)
        return {"ok": True, "value": {"elements": [{"name": "Go", "ref": str(i)} for i in range(matches)]}}

    with mechanical.prototype(tmp_path, "basic", enabled=True):
        result = NodeWorkerRuntime().execute("return await tools.browser_click_named({name:'Go'});",
            catalog=[ToolCatalogEntry(name, name) for name in ("browser_snapshot", "browser_click")], invoke=invoke)
    assert not result.ok
    assert calls == ["browser_snapshot"]


def test_no_helper_advertises_a_missing_capability(tmp_path: Path) -> None:
    snapshot = catalog.build_catalog_snapshot([ToolCatalogEntry("browser_read", "Read body")])
    with mechanical.prototype(tmp_path, "basic", enabled=True):
        text = catalog.render_declarations(snapshot)
    assert '"text": string' in text
    assert "browser_property" not in text and "_named(" not in text and "edit(args:" not in text


def read_result(text: str, *, ok: bool = True) -> ToolResult:
    return ToolResult(ToolCall("browser_read", {}, call_id="read-call"), "display is not source text", ok=ok,
                      canonical={"research_observation": {"text": text}})


def record(result: ToolResult, children: list[str] | None = None) -> ExecutionRecord:
    row = ExecutionRecord(name=result.call.name, ok=result.ok, call_id=result.call.call_id, excerpt=result.model_text,
        args_digest="unused", argument_digests={key: argument_digest(value) for key, value in result.call.args.items()})
    if children is not None:
        row["child_effect_ids"] = children
    return row


@pytest.mark.parametrize("defect", ["missing", "failed", "arguments", "identity", "conflict", "selector", "truncated"])
def test_read_binding_fails_closed(tmp_path: Path, defect: str) -> None:
    result = read_result("actual", ok=defect != "failed")
    if defect == "truncated":
        result = ToolResult(result.call, result.model_text, ok=True,
                            canonical={"research_observation": {"text": "partial", "source": {"truncated": True}}})
    row = record(result)
    if defect == "arguments":
        row["argument_digests"] = {"offset": "invented"}
    if defect == "identity":
        row["call_id"] = "different"
    rows = [("read", row)]
    results = {} if defect == "missing" else {"read": result}
    if defect == "conflict":
        other = read_result("different body")
        rows.append(("other", record(other)))
        results["other"] = other
    requirement = ToolSuccessRequirement("browser_read", answer_pointer="/body", result_pointer="/missing" if defect == "selector" else "")
    observation = mechanical.read_observation(requirement, rows, results)
    assert not observation.evaluation.passed
    assert "value" not in result.canonical


def test_source_binding_requires_successful_parent_and_uses_original_body(tmp_path: Path) -> None:
    result = read_result('real "body"\nnext line')
    parent = ToolResult(ToolCall("codemode", {}, call_id="parent-call"), "failed", ok=False)
    records = {"read": record(result), "parent": record(parent, ["read"])}
    requirement = ToolSuccessRequirement("browser_read", via="codemode", answer_pointer="/body")
    with mechanical.prototype(tmp_path, "upload_bound", enabled=True):
        failed = requirement.observe(records, results={"read": result})
        assert failed.evaluation.reason_code == "tool_parent_missing"
        records["parent"]["ok"] = True
        success = requirement.observe(records, results={"read": result})
    assert success.evaluation.passed and success.value == 'real "body"\nnext line'
    assert success.result_refs == ("read",) and "value" not in result.canonical


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("assembled", [False, True])
def test_upload_delivers_actual_receipt_facts_without_model_copying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, assembled: bool,
) -> None:
    attachment = InputAttachmentStore(tmp_path / "inputs", "upload-test").put(
        "fixture.txt", base64.b64encode(b"UPLOAD_BYTES_82\n").decode())
    code = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
            "unwrap(await tools.browser_upload_named({name:'Attachment',attachment_id:inputs.attachment}));"
            "unwrap(await tools.browser_wait({condition:'text',value:'UPLOAD_BYTES_82',timeout_ms:5000}));"
            "return {page_text:unwrap(await tools.browser_read({})).text};")
    install_http(monkeypatch, [
        {"tool": "codemode", "args": {"code": code, "inputs": {"url": URL, "attachment": str(attachment["id"])}}},
        {"tool": "done", "args": {"summary": "页面显示已选择的附件，尚未核验服务器接收。"}},
    ])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, "upload_bound" if assembled else "upload", "B",
                        Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10), max_turns=4, mechanical=True)
    assert row["passed"] and row["model_calls"] == 2, row
    assert "UPLOAD_BYTES_82" in str(row["answer"])
    receipts = tool_receipts(directory / "state")
    assert len([r for r in receipts if r.result.call.name == "browser_upload"]) == 1
    assert all("value" not in r.result.canonical for r in receipts if r.result.call.name == "browser_read")
    if not assembled:
        assert '"effect_verified": false' in str(row["answer"])


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_result_encoding_recovery_uses_receipts_without_second_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = InputAttachmentStore(tmp_path / "inputs", "upload-test").put(
        "fixture.txt", base64.b64encode(b"UPLOAD_BYTES_82\n").decode())
    code = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
            "unwrap(await tools.browser_upload_named({name:'Attachment',attachment_id:inputs.attachment}));"
            "unwrap(await tools.browser_wait({condition:'text',value:'UPLOAD_BYTES_82',timeout_ms:5000}));"
            "unwrap(await tools.browser_read({})); return {body:undefined};")
    install_http(monkeypatch, [
        {"tool": "codemode", "args": {"code": code, "inputs": {"url": URL, "attachment": str(attachment["id"])}}},
        {"tool": "done", "args": {"summary": "已选择附件；服务器接收尚未核验。"}},
    ])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, "upload_bound", "B", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
                        max_turns=4, mechanical=True)
    # Production recovers a failed return directly from successful child
    # receipts, before the experiment provider can propose a replay repair.
    assert row["passed"] and row["mechanical_repairs"] == 0 and row["model_calls"] == 2, row
    receipts = tool_receipts(directory / "state")
    assert len([r for r in receipts if r.result.call.name == "browser_upload"]) == 1
    parent, = [r.result for r in receipts if r.result.call.name == "codemode"]
    assert parent.ok and parent.canonical.get("recovered_return") is True
    assert parent.audit["original_error_code"] == "result_encoding"
    assert [item["receipt_ref"] for item in parent.canonical["value"]["replayed"]] == parent.canonical["child_effect_ids"]
    assert row["first_tool_rejection"] is None


@pytest.mark.parametrize("defect", ["runtime", "failed-child", "already-attempted", "missing-child", "edit"])
def test_automatic_repair_does_not_claim_semantic_or_execution_recovery(defect: str) -> None:
    child = ToolResult(ToolCall("edit" if defect == "edit" else "browser_upload", {}), "receipt", ok=defect != "failed-child")
    parent = ToolResult(ToolCall("codemode", {}), "failed", ok=False, canonical={
        "repair": {"token": "token", "failure_code": "runtime_error" if defect == "runtime" else "result_encoding", "parent_effect_id": "parent"},
        "child_effect_ids": ["child"],
    })
    receipts = [EffectReceipt("parent", parent, None)]
    if defect != "missing-child":
        receipts.append(EffectReceipt("child", child, None))
    assert mechanical.repair_action(receipts, {"token"} if defect == "already-attempted" else set()) is None


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_legacy_report_uses_committed_token_even_when_model_omits_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code = ("unwrap(await tools.browser_navigate({url:inputs.url}));"
            "unwrap(await tools.browser_fill_named({name:'Locality',value:'福州'}));"
            "unwrap(await tools.browser_click_named({name:'Load record'}));"
            "unwrap(await tools.browser_wait({condition:'text',value:'鱼丸',timeout_ms:5000}));"
            "const body=unwrap(await tools.browser_read({})).text;"
            "unwrap(await tools.browser_property({name:'__contractToken'}));"
            "unwrap(await tools.browser_tab_list({})); return {body};")
    install_http(monkeypatch, [{"tool": "codemode", "args": {"code": code, "inputs": {"url": URL}}},
                              {"tool": "done", "args": {"summary": "已读取，token 从 window.__contractToken 读取。"}}])
    directory = tmp_path / "run"
    row = probe.run_arm(directory, "legacy_structured", "B", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
                        max_turns=4, mechanical=True)
    assert row["passed"] and "SCHEMA_TOKEN_74" in str(row["answer"]), row
    assert row["mechanical_reports"] == 1
    proposals = (directory / "proposals.jsonl").read_text(encoding="utf-8")
    assert "SCHEMA_TOKEN_74" not in proposals


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_required_source_is_attempted_before_model_explanation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_http(monkeypatch, [{"tool": "done", "args": {"summary": "必要来源返回 HTTP 403，无法读取正文。"}}])
    row = probe.run_arm(tmp_path / "run", "source_failure", "B", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
                        max_turns=4, mechanical=True)
    assert row["passed"] and row["stop_reason"] == "blocked" and row["model_calls"] == 1, row
    assert row["mechanical_source_attempts"] == 1 and row["acquisition"] == "mechanical-required-open-url"
