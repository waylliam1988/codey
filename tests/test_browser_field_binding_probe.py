"""Run receipt field selection through real Chromium and production file writes."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_browser_entry_candidates import action, done
from tests.test_protocol_surface import TARGET
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_field_binding_probe as probe
from tools.completion_artifact_audit import tool_receipts

SELECTORS = {
    "text": {"prefix": "Net mass = ", "suffix": " g", "type": "integer"},
    "table": {"id": "orders", "key_column": "Order", "key": "A-102",
              "column": "Due", "type": "integer"},
    "json": {"id": "shipment", "pointer": "/shipping/trackingNumber", "type": "string"},
}


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize(("name", "defect", "error"), [
    ("text", "", ""),
    ("table", "", ""),
    ("json", "", ""),
    ("text", "duplicate_text", "expected one browser text prefix"),
    ("table", "duplicate_row", "Expected exactly one table row, found 2"),
    ("json", "reload", "current document"),
    ("json", "type", "declared type"),
    ("json", "duplicate_json", "duplicate answer JSON property"),
    ("json", "arbitrary_eval", "fixed browser projection"),
    ("json", "failed_parent", "complete committed receipt"),
])
def test_generic_selector_with_real_browser_and_versioned_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, defect: str, error: str,
) -> None:
    case = probe.CASES[name]
    selector = dict(SELECTORS[name])
    if defect == "duplicate_text":
        case = replace(case, html=case.html + "<p>Net mass = 999 g</p>")
    elif defect == "duplicate_row":
        case = replace(case, html=case.html.replace("</tbody>", "<tr><td>A-102</td><td>30</td><td>40</td></tr></tbody>"))
    elif defect == "type":
        selector["pointer"] = "/shipping"
    elif defect == "duplicate_json":
        case = replace(case, html=case.html.replace('"trackingNumber":"0042091"',
                                                  '"trackingNumber":"0042091","trackingNumber":"999"'))
    elif defect == "arbitrary_eval":
        synthetic = '[{id:"shipment",value:{shipping:{trackingNumber:"999"}}}]'
        case = replace(case, acquisition=f"tools.browser_eval({{code:{json.dumps(synthetic)}}})")

    navigate = f"unwrap(await tools.browser_navigate({{url:{json.dumps(case.url)}}}));"
    acquisition = f"{navigate}const r=await {case.acquisition};"
    if defect == "reload":
        acquisition += navigate
    acquisition += "await store('source',r.result_ref);"
    acquisition += ("throw Error('parent failed');" if defect == "failed_parent"
                    else "return {ref:r.result_ref,data:unwrap(r)};")
    field, = case.expected
    edit = (f"const read=await tools.read_file({{path:{json.dumps(case.path)}}});"
            f"const location={{...{json.dumps(selector)},ref:await load('source')}};"
            f"return unwrap(await tools.edit({{read,changes:{{{json.dumps(field)}:location}}}}));")
    install_http(monkeypatch, [action(acquisition), action(edit), action(edit), done("检查完成。")])
    directory = tmp_path / "run"
    row = probe.run_case(directory, case, TARGET, max_turns=4)
    assert row["passed"] is (not defect), row
    assert row["backup_preserved"] is True
    receipts = tool_receipts(directory / "state")
    edits = [item for item in receipts if item.result.call.name == "edit"]
    bindings = row["bindings"]
    assert isinstance(bindings, list) and len(bindings) == 2
    if not defect:
        assert row["true_stale_conflicts"] == 1 and row["fresh_read_recovery"] is True
        assert row["unchanged_on_conflict"] is True
        assert [item.result.ok for item in edits] == [False, True]
    else:
        assert not edits
        assert all(isinstance(item, dict) and item.get("status") == "rejected" and error in str(item.get("reason"))
                   for item in bindings)
        assert isinstance(disk := row["project_result"], dict) and disk[field] is None
