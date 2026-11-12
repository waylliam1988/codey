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
from codey.types.json_value import JsonObject, decode_json_object
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
@pytest.mark.parametrize("case", ["ordinary", "strict", "ambiguous", "expired", "premature"])
def test_observed_file_controls_are_selected_without_model_javascript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    data = b"UPLOAD_BYTES_82\n"
    attachment = InputAttachmentStore(tmp_path / "inputs", "structured-upload-test").put(
        "fixture.txt", base64.b64encode(data).decode("ascii"))
    identity = str(attachment["id"])
    directory = tmp_path / "run"
    strict = case == "strict"
    provider = BrowserSearchProvider(headless=True) if case == "expired" else None
    if provider is not None:
        monkeypatch.setattr("tools.browser_production_live_gate.BrowserSearchProvider", lambda **_kwargs: provider)
    html = HTML
    if case == "ambiguous":
        html = HTML.replace('<label>Attachment<input type="file"',
            '<fieldset><legend>Invoice</legend><label>Attachment<input type="file" id="invoice"></label></fieldset>'
            '<fieldset><legend>Receipt</legend><label>Attachment<input id="receipt" type="file"').replace(
            '</label>\n<p id="uploaded">', '</label></fieldset>\n<p id="uploaded">')

    def replies() -> Iterator[JsonObject]:
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
    monkeypatch.setattr("codey.app.provider_services.provider_tab_availability", lambda *_args, **_kwargs: {})
    monkeypatch.setattr("codey.app.review_service.run_review", lambda *_args, **_kwargs: None)
    result = run_case(directory, "chat", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
        upload_file=True, assembled_upload=strict, upload_navigation_required=strict, fixture_html=html, max_turns=7,
        restore_snapshots=True)
    assert result["passed"] is True, result
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
    requests = [json.loads(line) for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
    first = json.dumps(next(row["payload"] for row in requests if row.get("phase") == "request"))
    assert "Structured attachment selection" in first and "declare function unwrap" not in first
