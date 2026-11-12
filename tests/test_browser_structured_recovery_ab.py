"""Exercise structured recovery with real browser and project receipts."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from codey.types.json_value import JsonObject, decode_json_object
from tests.test_browser_entry_candidates import done
from tests.test_protocol_surface import TARGET
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_structured_recovery_ab as ab
from tools.browser_structured_edit_ab import EDIT_CODE
from tools.completion_artifact_audit import tool_receipts


def test_target_and_type_follow_only_the_task_grant() -> None:
    task = "授权：从 https://example.com/new 读取，只写 new.json 的 amount，类型 integer。"
    grant = ab.Grant.from_task(task)
    step = ab.Step("json", ("doc",))
    action: JsonObject = {"tool": "edit", "args": {"source": {"pointer": "/count"},
        "target": {"file": "new.json", "field": "amount"}}}
    assert ab.lower(action, grant, step)["args"] == {"code": EDIT_CODE, "inputs": {
        "file": "new.json", "field": "amount", "selector": {"id": "doc", "pointer": "/count", "type": "integer"}}}
    with pytest.raises(ValueError, match="one explicit"):
        ab.Grant.from_task(task + "\n" + task)
    with pytest.raises(ValueError, match="one explicit"):
        ab.Grant.from_task("写 new.json 的某个字段")
    invalid: list[JsonObject] = [
        {"source": {"pointer": "/count", "type": "string"}, "target": {"file": "new.json", "field": "amount"}},
        {"source": {"pointer": "/count"}, "target": {"file": "other.json", "field": "amount"}},
        {"source": {"pointer": "/count"}, "target": {"file": "new.json", "field": "other"}},
    ]
    for bad in invalid:
        with pytest.raises(ValueError):
            ab.lower({"tool": "edit", "args": bad}, grant, step)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("mode", [
    "single", "multiple", "wrong_key", "wrong_column", "wrong_after_conflict", "duplicate", "mixed", "json", "expired",
])
def test_recovery_is_driven_by_browser_and_edit_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    table = mode not in {"json", "expired"}
    case = ab.TABLE_CASE if table else ab.JSON_CASE
    source: JsonObject = ({"key_column": "Lot", "key": "B-17", "column": "Payable"} if table else
                          {"pointer": "/parcel/label"})
    if mode == "single":
        case = replace(case, html="<table><caption>Retail</caption><thead><tr><th>Lot</th><th>Payable</th></tr></thead>"
                       "<tbody><tr><td>B-17</td><td>962</td></tr></tbody></table>")
    elif table:
        source["id"] = "table-2"
    if mode == "duplicate":
        case = replace(case, html=case.html.replace("<tr><td>B-17</td><td>962</td></tr>",
                                                   "<tr><td>B-17</td><td>962</td></tr><tr><td>B-17</td><td>963</td></tr>"))
    if mode == "mixed":
        case = replace(case, html=case.html + '<script id="record" type="application/json">{"amount":1}</script>')
    grant = ab.Grant.from_task(case.task)
    observe: JsonObject = {"tool": "observe", "args": {"url": grant.url}}
    target: JsonObject = {"file": grant.file, "field": grant.field}
    edit: JsonObject = {"tool": "edit", "args": {"source": source, "target": target}}
    proposals = [observe]
    if mode in {"wrong_key", "wrong_column"}:
        wrong = {"key": "missing"} if mode == "wrong_key" else {"column": "missing"}
        proposals.append({"tool": "edit", "args": {"source": {**source, **wrong}, "target": target}})
    if mode == "wrong_after_conflict":
        proposals.extend([edit, {"tool": "edit", "args": {"source": {**source, "key": "missing"}, "target": target}}, edit])
    else:
        proposals.extend([edit, observe, edit] if mode == "expired" else [edit, edit])
    proposals.append(done("已写入指定字段。"))
    install_http(monkeypatch, proposals)
    directory = tmp_path / "run"
    row = ab.run_arm(directory, case, "B", TARGET, max_turns=len(proposals), expire=mode == "expired")
    assert row["passed"] is (mode not in {"duplicate", "mixed"}), row
    assert row["backup_preserved"] is True
    receipts = tool_receipts(directory / "state")
    edits = [item.result for item in receipts if item.result.call.name == "edit"]
    actions = [json.loads(line) for line in (directory / "recovery-actions.jsonl").read_text(encoding="utf-8").splitlines()]
    if mode in {"duplicate", "mixed"}:
        assert not edits
        reason = ("Expected exactly one table row, found 2" if mode == "duplicate" else
                  "Expected exactly one complete supported source format")
        assert any(reason in r.result.model_text for r in receipts)
        return
    assert isinstance(disk := row["project_result"], dict)
    assert disk[grant.field] == case.expected[grant.field] and "external_revision" in disk
    if mode == "expired":
        assert [item.ok for item in edits] == [True]
        assert [row["phase"] for row in actions] == ["observe", "json", "observe", "json", "done"]
        assert "document changed" in actions[2]["context"]
        bindings = row["bindings"]
        assert isinstance(bindings, list) and isinstance(bindings[0], dict) and isinstance(bindings[1], dict)
        assert bindings[0]["kind"] == "source_expired"
        selected = decode_json_object(decode_json_object(bindings[1]["selectors"])[grant.field])
        assert bindings[0]["source_ref"] != selected["ref"]
    else:
        assert [item.ok for item in edits] == [False, True]
        assert row["true_stale_conflicts"] == 1 and row["fresh_read_recovery"] is True
        assert row["unchanged_on_conflict"] is True
        assert "File version conflict" in actions[-3 if mode == "wrong_after_conflict" else -2]["context"]
        if mode in {"wrong_key", "wrong_column"}:
            assert "Correct the source selector" in actions[2]["context"]
            assert ("B-17" if mode == "wrong_key" else "Payable") in actions[2]["context"]
        elif mode == "wrong_after_conflict":
            assert "Correct the source selector" in actions[-2]["context"]
