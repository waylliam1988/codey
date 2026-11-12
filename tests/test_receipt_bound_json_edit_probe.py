import json
from pathlib import Path

import pytest

from codey.types.json_value import JsonObject
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import receipt_bound_json_edit_probe as probe
from tools.completion_artifact_audit import tool_receipts
from tools.local_model_gate_attempts import GateTarget


def test_patch_preserves_untouched_text_and_exact_numbers() -> None:
    text = '  { "code":"old","keep":{"id":9007199254740993,"rate":0.1000000000000000001}, "amount":0 }\n'
    assert probe.patch_fields(text, probe.FIELDS) == text.replace('"old"', '"QUARTZ_851"').replace('"amount":0', '"amount":173')


@pytest.mark.parametrize("text", [
    '[]', '{"amount": 1}', '{"code": "old", "amount": 1, "amount": 2}',
    '{"code": "old", "amount": 1, "keep":{"x":1,"x":2}}', '{"code": "old", "amount": NaN}', '{"code":',
])
def test_patch_rejects_invalid_or_ambiguous_documents(text: str) -> None:
    with pytest.raises(ValueError):
        probe.patch_fields(text, probe.FIELDS)


def test_patch_rejects_unrequested_fields_and_wrong_types() -> None:
    text = '{"code": "old", "amount": 1, "keep": false}'
    cases: list[JsonObject] = [{"amount": True, "code": "new"}, {"amount": 1, "code": "new", "keep": True}]
    for changes in cases:
        with pytest.raises(ValueError, match="changes must contain"):
            probe.patch_fields(text, changes)


@pytest.mark.usefixtures("scripted_local_api_connection")
@pytest.mark.parametrize(("arm", "unsafe_full_text", "write_instruction_ab"), [
    ("A", False, False), ("A", True, False), ("B", False, False), ("A", False, True), ("B", False, True),
])
def test_real_conflict_and_local_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str, unsafe_full_text: bool, write_instruction_ab: bool,
) -> None:
    begin = "const r=await tools.read_file({path:'result.json'});"
    values = "{amount:173,code:'QUARTZ_851'}"
    field_edit = write_instruction_ab or arm == "B"
    if not field_edit:
        original = "unwrap(r)" if unsafe_full_text else "JSON.parse(unwrap(r).text)"
        args = f"read:r,content:JSON.stringify({{...{original},...{values}}})"
    else:
        args = f"read:r,changes:{values}"
    code = begin + f"return unwrap(await tools.edit({{{args}}}));"
    edit: JsonObject = {"tool": "codemode", "args": {"code": code}}
    install_http(monkeypatch, [edit, edit, {"tool": "done", "args": {"summary": "Updated result.json."}}])
    directory = tmp_path / "run"
    target = GateTarget("http://scripted.test/v1", "scripted-fixture", 32768, 8192, 12000, protocol="text")
    row = probe.run_arm(directory, arm, target, max_turns=3, write_instruction_ab=write_instruction_ab)
    assert row["true_stale_conflicts"] == 1, row
    assert row["fresh_read_recovery"] is True
    assert row["rejected_edit_left_file_unchanged"] is True
    assert (directory / "after-edit-1.json").read_bytes() == (directory / "injected.json").read_bytes()
    assert row["confirmed_helper_edits"] == 1
    assert row["passed"] is (not unsafe_full_text), row
    if write_instruction_ab:
        transcript = [json.loads(line) for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
        requests = [json.dumps(record["payload"], ensure_ascii=False) for record in transcript if record["type"] == "request"]
        assert len(requests) == 3
        assert all("changes: {amount: number; code: string}" in request for request in requests)
        assert all(("持久化步骤：" in request) is (arm == "B") for request in requests)
    if field_edit:
        assert (directory / "after-edit-2.json").read_text(encoding="utf-8") == probe.patch_fields(
            (directory / "injected.json").read_text(encoding="utf-8"), probe.FIELDS)
        reads = [r.identity for r in tool_receipts(directory / "state") if r.result.call.name == "read_file"]
        edits = [r.result for r in tool_receipts(directory / "state") if r.result.call.name == "edit"]
        assert [r.call.args["base_result_ref"] for r in edits] == reads
        assert [r.ok for r in edits] == [False, True]
