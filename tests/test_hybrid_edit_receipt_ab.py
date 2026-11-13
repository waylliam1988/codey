"""Real production read/edit/version checks for the isolated credential interface."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.types.json_value import JsonObject, decode_json_object, parse_json_object
from tools.hybrid_edit_receipt_ab import Arm, FileExperiment, action_schema, cases


@pytest.mark.parametrize("case_index", [0, 1])
@pytest.mark.parametrize("arm", ["A", "B"])
def test_observed_read_conflicts_then_explicit_reread_preserves_external_fields(
    tmp_path: Path, case_index: int, arm: str,
) -> None:
    case = cases()[case_index]
    experiment = FileExperiment(tmp_path / "run", case.initial)
    initial = experiment.read()
    assert initial["ok"] is True and experiment.seen is not None
    old_ref = experiment.seen.ref
    experiment.inject_external_change()
    changes: JsonObject = {key: value for key, value in case.expected.items() if key != "metadata"}
    first: JsonObject = {"changes": changes}
    if arm == "A":
        first["base_result_ref"] = old_ref
    selected_arm: Arm = "A" if arm == "A" else "B"
    refused = experiment.edit(first, selected_arm)
    assert refused["ok"] is False
    edits = [r for r in experiment.session._memory_results.values() if r.call.name == "edit"]
    assert len(edits) == 1 and edits[0].call.args["base_result_ref"] == old_ref
    assert sum(r.call.name == "read_file" for r in experiment.session._memory_results.values()) == 1
    assert experiment.seen.ref == old_ref
    with pytest.raises(ValueError, match="after conflict read it again"):
        experiment.edit(first, selected_arm)
    fresh = experiment.read()
    assert fresh["ok"] is True and experiment.seen.ref != old_ref
    assert "external_revision" in experiment.seen.text
    second: JsonObject = {"changes": changes}
    if arm == "A":
        second["base_result_ref"] = experiment.seen.ref
    assert experiment.edit(second, selected_arm)["ok"] is True
    result = experiment.finish(case, done=True)
    assert result["passed"] is True, result
    assert result["true_stale_conflicts"] == 1 and result["read_calls"] == 2
    assert result["fresh_read_recovery"] is True and result["external_preserved"] is True


def test_original_unwrapped_read_is_rejected_before_version_check_then_bound_edit_conflicts(tmp_path: Path) -> None:
    case = cases()[0]
    experiment = FileExperiment(tmp_path / "run", case.initial)
    experiment.read()
    experiment.inject_external_change()
    wrong = ("const r=await tools.read_file({path:'result.json'});"
             "return unwrap(await tools.edit({read:unwrap(r),changes:{amount:173,code:'QUARTZ_851'}}));")
    refused = experiment.offer(experiment.execute(wrong))
    assert refused["ok"] is False and "Do not unwrap read" in str(refused["text"])
    # A hidden fresh read in a failed program must not replace what the model saw.
    assert experiment.seen is not None and "external_revision" not in experiment.seen.text
    assert not [r for r in experiment.session._memory_results.values() if r.call.name == "edit"]
    assert experiment.edit({"changes": {"amount": 173, "code": "QUARTZ_851"}}, "B")["ok"] is False
    experiment.read()
    assert experiment.edit({"changes": {"amount": 173, "code": "QUARTZ_851"}}, "B")["ok"] is True
    assert experiment.finish(case, done=True)["passed"] is True


@pytest.mark.parametrize("program", [
    "await tools.read_file({path:'result.json'});return null;",
    "return await tools.read_file({path:'result.json',limit:1});",
    "return await tools.read_file({path:'backup.json'});",
])
def test_hidden_partial_or_other_file_read_cannot_supply_credential(tmp_path: Path, program: str) -> None:
    # Multiple lines make limit:1 an actually incomplete read.
    experiment = FileExperiment(tmp_path / "run", cases()[0].initial)
    (experiment.project / "result.json").write_text('{\n"amount":0,\n"code":"unknown"\n}\n', encoding="utf-8")
    experiment.offer(experiment.execute(program))
    assert experiment.seen is None
    with pytest.raises(ValueError, match="complete result.json"):
        experiment.edit({"changes": {"amount": 173}}, "B")
    assert not [r for r in experiment.session._memory_results.values() if r.call.name == "edit"]


def test_read_execution_alone_does_not_advance_displayed_credential(tmp_path: Path) -> None:
    experiment = FileExperiment(tmp_path / "run", cases()[0].initial)
    experiment.read()
    assert experiment.seen is not None
    initial = experiment.seen
    experiment.inject_external_change()
    experiment.execute("return await tools.read_file({path:'result.json'});")
    assert experiment.seen == initial
    assert experiment.edit({"changes": {"amount": 173, "code": "QUARTZ_851"}}, "B")["ok"] is False


@pytest.mark.parametrize("defect", ["missing", "copied_data", "wrong_ref", "override", "new_field", "tampered"])
def test_invalid_credential_or_fields_cannot_write(tmp_path: Path, defect: str) -> None:
    experiment = FileExperiment(tmp_path / "run", cases()[0].initial)
    experiment.read()
    assert experiment.seen is not None
    args: JsonObject = {"changes": {"amount": 173, "code": "QUARTZ_851"}}
    arm = "A" if defect in {"missing", "copied_data", "wrong_ref"} else "B"
    if defect == "copied_data":
        args["base_result_ref"] = {"amount": 0, "code": "unknown"}
    elif defect == "wrong_ref":
        args["base_result_ref"] = "invented"
    elif defect == "override":
        args["base_result_ref"] = experiment.seen.ref
    elif defect == "new_field":
        args["changes"] = {"unknown": "new"}
    elif defect == "tampered":
        experiment.seen = replace(experiment.seen, text="{}")
    before = (experiment.project / "result.json").read_bytes()
    with pytest.raises(ValueError):
        experiment.edit(args, "A" if arm == "A" else "B")
    assert (experiment.project / "result.json").read_bytes() == before
    assert not [r for r in experiment.session._memory_results.values() if r.call.name == "edit"]


def test_schemas_differ_only_by_the_edit_credential_argument() -> None:
    baseline = action_schema("A")
    candidate = action_schema("B")
    variants = baseline["anyOf"]
    assert isinstance(variants, list)
    edit = decode_json_object(variants[1])
    properties = decode_json_object(edit["properties"])
    args = decode_json_object(properties["args"])
    fields = decode_json_object(args["properties"])
    del fields["base_result_ref"]
    args["properties"] = fields
    args["required"] = ["changes"]
    properties["args"] = args
    edit["properties"] = properties
    variants[1] = edit
    assert baseline == candidate


@pytest.mark.parametrize("name", ["nested_changes", "correct_changes"])
def test_saved_live_replies_separate_field_shape_failure_from_conflict_recovery(tmp_path: Path, name: str) -> None:
    fixture = parse_json_object((Path(__file__).parent / "fixtures" / "hybrid_edit_receipt_replies_20261113.json").read_text(
        encoding="utf-8"))
    replies = decode_json_object(fixture[name])["replies"]
    assert isinstance(replies, list)
    case = cases()[1]
    experiment = FileExperiment(tmp_path / "run", case.initial)
    experiment.read()
    experiment.inject_external_change()
    errors: list[str] = []
    done = False
    for raw in replies:
        assert isinstance(raw, str)
        reply = parse_json_object(raw)
        if reply["tool"] == "done":
            done = True
            break
        try:
            experiment.dispatch(reply, "B")
        except ValueError as exc:
            errors.append(str(exc))
    result = experiment.finish(case, done=done)
    assert result["passed"] is (name == "correct_changes"), result
    if name == "nested_changes":
        assert len(errors) == 5 and all("fields do not exist" in error for error in errors)
        assert result["true_stale_conflicts"] == 0 and result["field_edits"] == 0
        assert result["external_preserved"] is True
    else:
        assert not errors and result["true_stale_conflicts"] == 1 and result["fresh_read_recovery"] is True
