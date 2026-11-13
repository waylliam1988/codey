"""Structured upload through production admission, Chromium and committed receipts."""
from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession
from codey.research.browser_search import BrowserSearchProvider
from codey.storage.input_attachments import InputAttachmentStore
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_production_live_gate import HTML, URL, run_case
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target


def action(name: str, **args: str | JsonObject) -> JsonObject:
    return {"tool": "codemode", "args": {"upload": {"action": name, **args}}}


def observed_controls(directory: Path) -> list[JsonObject]:
    receipt = [row.result for row in tool_receipts(directory / "state")
               if row.result.ok and row.result.call.args.get("file_inputs_only") is True][-1]
    rows = decode_json_object(receipt.canonical["value"])["file_controls"]
    assert isinstance(rows, list)
    return [decode_json_object(row) for row in rows]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("case", ["ordinary", "strict", "ambiguous", "expired", "premature", "raw_code", "parent_return"])
def test_observed_file_controls_are_selected_without_model_javascript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    data = b"UPLOAD_BYTES_82\n"
    attachment = InputAttachmentStore(tmp_path / "inputs", "structured-upload-test").put(
        "fixture.txt", base64.b64encode(data).decode("ascii"))
    identity = str(attachment["id"])
    directory = tmp_path / "run"
    if case == "parent_return":
        from codey.operations import kernel_protocol
        from codey.operations.browser_upload import lower_upload_action

        original_lower = lower_upload_action

        def missing_return(args: JsonObject, checks: tuple[str, ...] = ()) -> JsonObject:
            converted = original_lower(args, checks)
            if decode_json_object(args["upload"])["action"] == "select":
                converted["code"] = str(converted["code"]).removeprefix("return ")
            return converted

        monkeypatch.setattr(kernel_protocol, "lower_upload_action", missing_return)
    strict = case == "strict"
    provider = BrowserSearchProvider(headless=True) if case == "expired" else None
    if provider is not None:
        fixed_provider = provider

        def current_browser(**_kwargs: object) -> BrowserSearchProvider:
            return fixed_provider

        monkeypatch.setattr("tools.browser_production_live_gate.BrowserSearchProvider", current_browser)
    html = HTML
    if case == "ambiguous":
        html = HTML.replace('<label>Attachment<input type="file"',
            '<fieldset><legend>Invoice</legend><label>Attachment<input type="file" id="invoice"></label></fieldset>'
            '<fieldset><legend>Receipt</legend><label>Attachment<input id="receipt" type="file"').replace(
            '</label>\n<p id="uploaded">', '</label></fieldset>\n<p id="uploaded">')

    def replies() -> Iterator[JsonObject]:
        if case == "raw_code":
            yield {"tool": "codemode", "args": {"code": "return null;"}}
        if case == "premature":
            yield action("select", attachment_id=identity, target={"name": "Attachment"})
        yield action("observe")
        controls = observed_controls(directory)
        if case == "ambiguous":
            assert [row["group"] for row in controls] == ["Invoice", "Receipt"]
            yield action("select", attachment_id=identity, target={"name": "Attachment"})
            target: JsonObject = {"id": controls[1]["id"]}
        elif case == "expired":
            assert provider is not None and BrowserSession.from_provider(provider).navigate(URL).ok
            yield action("select", attachment_id=identity, target={"id": controls[0]["id"]})
            yield action("observe", url=URL)
            yield action("select", attachment_id=identity, target={"id": controls[0]["id"]})
            fresh = observed_controls(directory)[0]["id"]
            assert fresh != controls[0]["id"]
            target = {"id": fresh}
        else:
            assert len(controls) == 1 and controls[0]["name"] == "Attachment"
            target = {"name": "Attachment"}
        yield action("select", attachment_id=identity, target=target)
        if case == "premature":
            yield action("observe", url=URL)
        yield action("read", wait_for_text="UPLOAD_BYTES_82")
        yield {"tool": "done", "args": {"summary": "已选择附件并检查页面。"}}

    install_http(monkeypatch, replies())

    def no_tabs(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {}

    def no_review(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("codey.app.provider_services.provider_tab_availability", no_tabs)
    monkeypatch.setattr("codey.app.review_service.run_review", no_review)
    result = run_case(directory, "chat", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
        upload_file=True, assembled_upload=strict, upload_navigation_required=strict, fixture_html=html, max_turns=7,
        restore_snapshots=True)
    assert result["passed"] is True, result
    if case == "parent_return":
        parents = [row.result for row in tool_receipts(directory / "state") if row.result.call.name == "codemode"]
        assert any(parent.canonical.get("recovered_return") is True for parent in parents)
    assert hashlib.sha256(data).hexdigest() in str(result["answer"])
    receipts = [row.result for row in tool_receipts(directory / "state")]
    attempts = [receipt for receipt in receipts if receipt.call.name == "browser_upload"]
    selected, = [receipt for receipt in attempts if receipt.ok]
    assert selected.canonical["value"] is not None and selected.audit.get("browser_document") is not None
    read, = [receipt for receipt in receipts if receipt.ok and receipt.call.name == "browser_read"]
    assert read.audit["browser_document"] == selected.audit["browser_document"]
    assert decode_json_object(selected.canonical["value"])["effect_verified"] is False
    rejected = [receipt for receipt in attempts if not receipt.ok]
    if case == "ambiguous":
        failure, = rejected
        assert '"dom_id": "invoice"' in failure.model_text and '"dom_id": "receipt"' in failure.model_text
    elif case == "expired":
        stale, failure = rejected
        assert decode_json_object(stale.canonical["value"])["reason"] == "file_observation_expired"
        assert "Candidates:" in failure.model_text
    else:
        assert not rejected
    for receipt in rejected:
        assert decode_json_object(receipt.canonical["value"])["delivery"] == "not_delivered"
    if case == "premature":
        assert next(receipt for receipt in receipts if receipt.call.name == "codemode").ok
        assert result["navigations"] == [URL]
        assert len([receipt for receipt in receipts if receipt.call.name == "browser_snapshot"]) == 1
    requests = [parse_json_object(line) for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
    first = json.dumps(next(row["payload"] for row in requests if row.get("phase") == "request"))
    assert "Structured attachment selection" in first and "declare function unwrap" not in first
