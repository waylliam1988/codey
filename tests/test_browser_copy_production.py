"""Default browser copy through production admission, provider and tool effects."""
import json
from pathlib import Path

import pytest
from playwright.sync_api import Route

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.automation.browser_session import BrowserSession
from codey.operations.browser_copy import EXPIRED, copy_target
from codey.research.browser_search import BrowserSearchProvider
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_protocol_surface import TARGET
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.completion_artifact_audit import artifacts, tool_receipts
from tools.completion_explanation_ab import ExperimentProvider, write_json
from tools.receipt_bound_edit_recovery_probe import receipt_audit

URL = "https://example.com/copy-record"
OBSERVE: JsonObject = {"tool": "codemode", "args": {}}
DONE: JsonObject = {"tool": "done", "args": {"summary": "已复制网页值，保留文件其他字段。"}}
TABLE = (
    "<table><caption>Wholesale</caption><thead><tr><th>Lot</th><th>Due</th></tr></thead>"
    "<tbody><tr><td>B-17</td><td>9112</td></tr></tbody></table>"
    "<table><caption>Retail</caption><thead><tr><th>Lot</th><th>Due</th></tr></thead>"
    "<tbody><tr><td>B-17</td><td>962</td></tr><tr><td>B-19</td><td>100</td></tr></tbody></table>"
)
JSON_PAGE = (
    '<script id="dispatch" type="application/json">{"parcel":{"label":"000783"}}</script>'
    "<article>Shipment is ready. The original formatting of the carrier tracking number is significant.</article>"
)


def test_copy_scope_is_explicit_and_narrow() -> None:
    ordinary = f"打开 {URL}，把网页上的运单号原样写入 `dispatch.json` 的 `carrier_label`，保留前导零。"
    target = copy_target(ordinary)
    assert target is not None and (target.url, target.path, target.field, target.value_type) == (
        URL, "dispatch.json", "carrier_label", "string")
    assert copy_target(f"Copy the value from {URL} into dispatch.json field carrier_label as a string.") == target
    assert copy_target(ordinary.replace("原样", "换算后")) is None
    assert copy_target(ordinary + " 也写入 other.json 的 id。") is None
    assert copy_target(ordinary.replace("保留前导零", "保持原样")) is None
    assert copy_target(ordinary.replace("写入", "写进")) == target
    assert copy_target(ordinary.replace("`carrier_label`", "`carrier_label` 和 `billing`")) is None
    assert copy_target(ordinary + " 然后运行测试。") is None
    assert copy_target(ordinary + " 先用 observe。") is None


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("case", [
    "table", "json", "expired", "changed_data", "changed_file", "wrong_selector", "wrong_target", "raw_code", "duplicate",
])
def test_browser_copy_default_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    table = case in {"table", "wrong_selector", "duplicate"}
    path, field = ("ledger.json", "payable") if table else ("dispatch.json", "carrier_label")
    task = (f"打开 {URL}，把 Retail 表中 B-17 的 Due 以整数原样写入 ledger.json 的 payable。"
            if table else f"打开 {URL}，把运单号原样写入 dispatch.json 的 carrier_label，保留前导零。")
    task += "保留其他字段。"
    selector: JsonObject = {"id": "table-2", "key_column": "Lot", "key": "B-17", "column": "Due"} if table else {"pointer": "/parcel/label"}
    edit: JsonObject = {"tool": "codemode", "args": {"source": selector, "target": {"file": path, "field": field}}}
    proposals: list[JsonObject] = (
        [{"tool": "codemode", "args": {"code": "return null;"}}, OBSERVE] if case == "raw_code" else [OBSERVE])
    if case == "wrong_selector":
        proposals.append({"tool": "codemode", "args": {"source": {**selector, "column": "Amount"},
                                                     "target": {"file": path, "field": field}}})
    elif case == "wrong_target":
        proposals.append({"tool": "codemode", "args": {"source": selector,
                                                     "target": {"file": path, "field": "parcel.label"}}})
    if case == "duplicate":
        proposals.extend([edit, {"tool": "ask_user", "args": {
            "question": "B-17 对应两行，应使用哪一行？", "options": ["Due 为 962", "Due 为 963"]}}])
    else:
        proposals.extend([edit, OBSERVE, edit] if case in {"expired", "changed_data"} else [edit, edit])
        proposals.append(DONE)
    install_http(monkeypatch, proposals)
    project = tmp_path / "project"
    project.mkdir()
    file = project / path
    initial: JsonObject = {field: None, "keep": {"key": "00017"}}
    write_json(file, initial)
    backup = project / "backup.json"
    backup.write_bytes(file.read_bytes())
    before_backup = backup.read_bytes()
    injected: bytes | None = None
    snapshots: list[bytes] = []
    active: BrowserSearchProvider | None = None

    html = TABLE if table else JSON_PAGE
    if case == "duplicate":
        html = html.replace("<tr><td>B-19</td><td>100</td></tr>", "<tr><td>B-17</td><td>963</td></tr>")

    def route(provider: BrowserSearchProvider, request: Route) -> None:
        nonlocal active
        active = provider
        allowed = request.request.url == URL
        request.fulfill(status=200 if allowed else 404, body=html if allowed else "", content_type="text/html; charset=utf-8")

    def observe(event: dict[str, object]) -> None:
        nonlocal injected, html
        if event.get("type") != "tool":
            return
        if event.get("tool_name") == "edit":
            snapshots.append(file.read_bytes())
            if case == "changed_file" and event.get("ok") is True:
                write_json(file, {**initial, "external": "changed again"})
        if (injected is None and event.get("tool_name") == "read_file" and event.get("path") == path
                and event.get("ok") is True):
            write_json(file, {**initial, "external": "keep"})
            injected = file.read_bytes()
            if case == "expired":
                assert active is not None
                assert BrowserSession.from_provider(active).navigate(URL).ok
            elif case == "changed_data":
                assert active is not None
                browser = BrowserSession.from_provider(active)
                document = browser.document_identity()
                assert browser.evaluate(
                    """document.querySelector("#dispatch").textContent='{"parcel":{"label":"000784"}}'"""
                ).ok
                assert browser.document_identity() == document
                html = html.replace("000783", "000784")

    monkeypatch.setattr(BrowserSearchProvider, "_guard_request", route)
    monkeypatch.setattr("codey.app.provider_services.provider_availability", lambda: {"local": True})
    monkeypatch.setattr("codey.app.provider_services.provider_tab_availability", dict)

    def unavailable_review(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("codey.app.review_service.run_review", unavailable_review)
    recorder = ExperimentProvider(TARGET, tmp_path, fixed_acquisition=False)
    state = tmp_path / "state"
    try:
        result = run_headless(HeadlessRequest(
            project, task, intent="hybrid", provider_id="local", max_turns=len(proposals), state_home=state,
            research_store_root=tmp_path / "knowledge",
            requested_capabilities=("web.read", "browser.observe", "browser.interact", "browser.effect"),
            model_selection={"model": TARGET.model, "base_url": TARGET.base_url},
        ), emit_jsonl=observe, connect_provider=lambda *_args, **_kwargs: recorder)
    finally:
        recorder.close()
    receipts = tool_receipts(state)
    edits = [item.result for item in receipts if item.result.call.name == "edit"]
    assert backup.read_bytes() == before_backup
    if case == "duplicate":
        assert result.stop_reason == "question" and not edits and injected is None
        return
    if case == "changed_file":
        assert result.stop_reason != "done"
        assert len(edits) == 2 and edits[-1].ok
        assert any("browser_copy_content_changed" in json.dumps(body) for _, body in artifacts(state))
        return
    assert result.stop_reason == "done", result
    assert len(edits) == 2 and [item.ok for item in edits] == [False, True]
    assert len(snapshots) == 2 and injected == snapshots[0]
    assert decode_json_object(json.loads(file.read_text(encoding="utf-8"))) == {
        **initial, field: 962 if table else "000784" if case == "changed_data" else "000783", "external": "keep"}
    previous, final = [decode_json_object(item.call.args["browser_source"]) for item in edits]
    if case in {"expired", "changed_data"}:
        assert edits[0].audit["error_code"] == EXPIRED
        assert previous["ref"] != final["ref"]
    else:
        audit = receipt_audit([(item.identity, item.result) for item in receipts])
        assert audit["true_stale_conflicts"] == 1 and audit["fresh_read_recovery"] is True
        assert previous["ref"] == final["ref"]
    requests = [json.loads(line) for line in (tmp_path / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
    schemas = [row["payload"]["response_format"]["json_schema"]["schema"] for row in requests if row.get("phase") == "request"]
    assert len(schemas) == len(proposals)
    assert all('"code"' not in json.dumps(schema) for schema in schemas)
