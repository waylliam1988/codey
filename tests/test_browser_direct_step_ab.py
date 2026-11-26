"""Check model projection against actual production lowering and receipts."""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from codey.operations.browser.turn_surface import offer_browser_tools
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.kernel.turn_contract import ModelInputBinding, TurnSnapshot
from codey.operations.kernel.turn_presentation import refresh_snapshot_contract
from codey.operations.kernel_prompt import _text_request_prompt
from codey.operations.project.declared_field_input import model_spec
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.research.ledger import ResearchLedger
from codey.research.passages import restore_passages
from codey.types.json_value import (
    JsonObject,
    canonical_json_object,
    decode_json_object,
    parse_json_object,
    require_json_integer,
)
from tests.test_research_submission_handoff_production import install_http, requests
from tools import browser_direct_step_ab as experiment
from tools.browser_choice_presentation_ab import CASES
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Arm, Target, append_json

TARGET = Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10)


def snapshot() -> TurnSnapshot:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"})),
                          task_kind="chat", task_text="查询当前记录")
    return offer_browser_tools(build_turn_snapshot(session), session)


def payload(current: TurnSnapshot) -> JsonObject:
    return {"model": "same-model", "seed": 17, "temperature": 0,
        "messages": [{"role": "system", "content": "Existing instructions\n" + _text_request_prompt("", current)},
                     {"role": "user", "content": "Original task"},
                     {"role": "assistant", "content": "Original failed program/history"}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "text_tool_call", "strict": True, "schema": experiment.schema_for(current)}}}


def test_view_hides_only_free_js_and_keeps_execution_snapshot_unchanged() -> None:
    original = snapshot()
    encoded = snapshot_description(original)
    candidate = experiment.model_view(original, "查询当前记录")
    assert set(original.tool_names) - set(candidate.tool_names) == {"codemode"}
    assert candidate.nested_tool_names == original.nested_tool_names
    assert candidate.policy is original.policy and candidate.allowed == original.allowed
    assert {spec.name: spec for spec in candidate.frozen_specs} == {spec.name: spec for spec in original.frozen_specs}
    assert dict(candidate.custom_executors) == dict(original.custom_executors)
    assert snapshot_description(original) == encoded and restore_snapshot(encoded) == original
    # Direct lowering still requires the original internal CodeMode declaration.
    action: JsonObject = {"tool": "browser_fill", "args": {"ref": "current-ref", "value": "福州"}}
    plan = normalize_turn(decode_text_turn(json.dumps(action)), snapshot=original)
    assert not plan.protocol_error and plan.calls[0].name == "codemode"
    assert "tools.browser_fill" in str(plan.calls[0].args["code"])


def test_request_changes_only_current_contract_and_sampling_schema() -> None:
    original = snapshot()
    before = payload(original)
    saved = canonical_json_object(before)
    after = experiment.project_request(before, original, "查询当前记录")
    old_messages, new_messages = before["messages"], after["messages"]
    assert isinstance(old_messages, list) and isinstance(new_messages, list)
    assert old_messages[1:] == new_messages[1:]
    assert {key: value for key, value in after.items() if key not in {"messages", "response_format"}} == {
        key: value for key, value in before.items() if key not in {"messages", "response_format"}}
    assert before == saved
    text = str(decode_json_object(new_messages[0])["content"])
    assert "Existing instructions" in text and "browser_fill" in text
    assert "Write an async JavaScript" not in text
    assert '"codemode"' not in json.dumps(after["response_format"])


@pytest.mark.parametrize("guard", ["explicit_js", "bound_copy", "no_free_js"])
def test_existing_special_contracts_are_never_removed(guard: str) -> None:
    original = snapshot()
    task = "Use codemode only." if guard == "explicit_js" else "查询当前记录"
    if guard == "bound_copy":
        spec = next(spec for spec in original.frozen_specs if spec.name == "codemode")
        original = replace(original, input_binding=ModelInputBinding(
            "declared_fields", model_spec(spec, ("result.json",)), ("verified-source",)))
    elif guard == "no_free_js":
        original = refresh_snapshot_contract(replace(original, tool_names=("done",)))
    assert experiment.model_view(original, task) is original
    before = payload(original)
    assert experiment.project_request(before, original, task) == before


@pytest.mark.parametrize("fault", ["missing_contract", "different_schema"])
def test_projection_rejects_a_stale_or_uncaptured_request(fault: str) -> None:
    original = snapshot()
    request = payload(original)
    if fault == "missing_contract":
        request["messages"] = [{"role": "system", "content": "other instructions"}]
    else:
        empty_schema: JsonObject = {}
        request["response_format"] = {"type": "json_schema", "json_schema": {"schema": empty_schema}}
    with pytest.raises(ValueError):
        experiment.project_request(request, original, "查询当前记录")


def test_provider_uses_final_request_snapshot_including_completion_repair(tmp_path: Path) -> None:
    current = snapshot()
    provider = experiment.DirectProvider(TARGET, tmp_path, "B", ())

    def latest() -> TurnSnapshot:
        return current

    provider.snapshot_source = latest
    provider.task = "查询当前记录"
    try:
        first: dict[str, object] = dict(payload(current))
        provider.configure_experiment(first)
        assert '"codemode"' not in json.dumps(first["response_format"])
        current = refresh_snapshot_contract(replace(current, tool_names=("done",)))
        repair: dict[str, object] = dict(payload(current))
        provider.configure_experiment(repair)
        assert repair["response_format"] == payload(current)["response_format"]
    finally:
        provider.close()


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("arm", ["A", "B"])
@pytest.mark.parametrize("name", ["visible", "reordered", "early_break", "partial", "file_copy"])
def test_scripted_production_run_completes_without_repeating_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: Arm, name: str,
) -> None:
    directory = tmp_path / "run"
    case = experiment.fixture(name, 0)

    def proposals() -> Iterator[JsonObject]:
        if arm == "A":
            setup = ""
            if name != "visible":
                if name != "partial":
                    setup += ("const s=unwrap(await tools.browser_snapshot({}));"
                        "unwrap(await tools.browser_fill({ref:s.elements.find(e=>e.role==='textbox').ref,value:inputs.city}));")
                setup += ("const fresh=unwrap(await tools.browser_snapshot({}));"
                    "unwrap(await tools.browser_click({ref:fresh.elements.find(e=>e.role==='button').ref}));")
            yield {"tool": "codemode", "args": {"code": setup + "return unwrap(await tools.browser_read({}));",
                                                 "inputs": {"city": "福州"}}}
        else:
            if name != "visible":
                yield {"tool": "browser_snapshot", "args": {}}
                if name != "partial":
                    label = "Destination" if name == "reordered" else "城市" if name == "file_copy" else "Locality"
                    yield {"tool": "browser_fill", "args": {
                        "ref": {"role": "textbox", "name": label}, "value": "福州"}}
                button = "Check availability" if name == "reordered" else "获取测量" if name == "file_copy" else "Load record"
                yield {"tool": "browser_click", "args": {"ref": {"role": "button", "name": button}}}
            yield {"tool": "browser_read", "args": {}}
        if name == "file_copy":
            yield {"tool": "edit", "args": {"path": "result.json"}}
        ledger = ResearchLedger()
        for row in tool_receipts(directory / "state"):
            if row.result.ok and row.result.call.name in {"browser_read", "open_url"}:
                restore_passages(ledger, row.result.canonical.get("research_observation"),
                                 row.result.canonical.get("read_passages"))
        reference = next(ref for ref, passage in ledger.read_passages.items() if case.code in passage.excerpt)
        yield {"tool": "done", "args": {"summary": f"净重 {case.amount} 克，校验码 {case.code} [{reference}]"}}

    install_http(monkeypatch, proposals(), native=False)
    result = experiment.run_arm(directory, case, arm, TARGET)
    assert result["passed"], result
    assert result["no_repeated_actions"] and result["view_violations"] == []
    wire = requests(directory)
    assert all("response_format" in row for row in wire)
    if name != "visible":
        first = json.dumps(wire[0]["messages"], ensure_ascii=False)
        assert case.code not in first
    if arm == "B":
        # The old failure receipt may quote its original JS contract. Preserve
        # that history; only this request's system declaration is projected.
        projections = [parse_json_object(line) for line in (directory / "projection.jsonl").read_text(
            encoding="utf-8").splitlines()]
        final_projections = {require_json_integer(row["exchange"], "exchange"): row for row in projections
                             if isinstance(decode_json_object(row["request_before_projection"]).get("response_format"), dict)}
        assert len(final_projections) == len(wire)
        for exchange, row in enumerate(wire, 1):
            projection = final_projections[exchange]
            if projection["bound_input"] is not None:
                assert projection["changed"] is False
                assert row["messages"] == decode_json_object(projection["request_before_projection"])["messages"]
                continue
            messages = row["messages"]
            assert isinstance(messages, list)
            assert all("Write an async JavaScript" not in str(message.get("content", ""))
                       for message in messages if isinstance(message, dict) and message.get("role") == "system")


def test_out_of_view_proposal_is_counted_as_failure_without_rewriting(tmp_path: Path) -> None:
    append_json(tmp_path / "projection.jsonl", {"exchange": 1, "model_tools": ["browser_read", "done"]})
    action: JsonObject = {"tool": "codemode", "args": {"code": "return 1;"}}
    append_json(tmp_path / "provider.jsonl", {"type": "response", "exchange": 1,
        "payload": {"choices": [{"message": {"content": json.dumps(action)}}]}})
    assert experiment.view_violations(tmp_path) == [{
        "exchange": 1, "tool": "codemode", "reason": "outside_model_view"}]


def test_natural_and_explicit_js_tasks_are_separate() -> None:
    for name in CASES:
        natural = experiment.fixture(name, 0)
        explicit = experiment.fixture(name, 0, explicit_js=True)
        assert "Use codemode only." not in natural.task
        assert explicit.task == natural.task + " Use codemode only."
        assert natural.body == explicit.body and natural.prefixes == explicit.prefixes
