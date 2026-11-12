"""Check the structured wire, real Chromium acquisition and versioned edits."""
import json
from pathlib import Path

import pytest

from codey.types.json_value import JsonObject
from tests.test_browser_entry_candidates import done
from tests.test_protocol_surface import TARGET
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_field_binding_probe as probe
from tools import browser_structured_edit_ab as ab
from tools.completion_artifact_audit import tool_receipts


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize(("case", "defect"), [("table", ""), ("json", ""), ("json", "target"), ("table", "id")])
def test_structured_proposals_preserve_the_real_edit_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, defect: str,
) -> None:
    fixture = probe.CASES[case]
    field, = fixture.expected
    source: JsonObject = ({"id": "orders", "key_column": "Order", "key": "A-102", "column": "Due", "type": "integer"}
              if case == "table" else {"id": "shipment", "pointer": "/shipping/trackingNumber", "type": "string"})
    observe: JsonObject = {"tool": "observe", "args": {"url": fixture.url, "format": case}}
    correct: JsonObject = {"tool": "edit", "args": {"source": source, "target": {"file": fixture.path, "field": field}}}
    wrong: JsonObject = {"tool": "edit", "args": {
        "source": {**source, "id": "task_turn_effect:fake"} if defect == "id" else source,
        "target": {"file": fixture.path, "field": "/shipping/trackingNumber" if defect == "target" else field},
    }}
    proposals = [observe, *([wrong] if defect else []), correct, correct, done("已写入目标字段。")]
    install_http(monkeypatch, proposals)
    directory = tmp_path / "run"
    row = ab.run_arm(directory, case, "B", TARGET, max_turns=len(proposals))
    assert row["passed"] is True, row
    assert row["true_stale_conflicts"] == 1 and row["fresh_read_recovery"] is True
    assert row["unchanged_on_conflict"] is True and row["backup_preserved"] is True
    assert row["structured_edits"] == 2
    actions = [json.loads(line) for line in (directory / "structured-actions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [action["original"] for action in actions] == proposals
    if defect:
        assert ("target.field" if defect == "target" else "source.id") in actions[1]["error"]
        assert actions[1]["lowered"]["args"]["code"] == "throw Error(inputs.reason);"
    requests = [json.loads(line) for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
    schemas = [row["payload"]["response_format"]["json_schema"]["schema"]
               for row in requests if row.get("phase") == "request"]
    assert len(schemas) == len(proposals)
    assert all('"code"' not in json.dumps(schema) and '"codemode"' not in json.dumps(schema) for schema in schemas)
    offered = [schema["allOf"][0]["properties"]["tool"]["enum"][0] for schema in schemas]
    assert offered == [proposal["tool"] for proposal in proposals]
    assert schemas[1]["allOf"][1]["properties"]["args"]["properties"]["source"]["properties"]["id"]["enum"] == [source["id"]]
    edits = [item.result for item in tool_receipts(directory / "state") if item.result.call.name == "edit"]
    assert [item.ok for item in edits] == [False, True]
    assert isinstance(content := edits[-1].call.args["content"], str)
    assert json.loads(content)[field] == fixture.expected[field]


def test_compiler_refuses_unscoped_operations_and_user_js() -> None:
    fixture = probe.CASES["table"]
    actions: list[JsonObject] = [
        {"tool": "observe", "args": {"url": "https://example.com/other", "format": "table"}},
        {"tool": "codemode", "args": {"code": "return null;"}},
    ]
    for action in actions:
        with pytest.raises(ValueError):
            ab.lower(action, fixture, "observe")
