"""Frozen declarations, examples and both protocols share one structural contract."""

import json
from dataclasses import replace

import pytest

from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.toolchain.tool_spec import tool_specs, validate_args_with_spec
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object


@pytest.mark.parametrize("purpose", ["ordinary", "evidence", "report"])
def test_every_visible_example_passes_frozen_spec_and_both_protocols(purpose: str) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "knowledge.read", "knowledge.write", "knowledge.link"}),
                                   strict_research=purpose == "report"),
                          task_kind="evidence_followup" if purpose == "evidence" else "research")
    session.research_questions = ("Main question",)
    snapshot = build_turn_snapshot(session)
    for spec in snapshot.frozen_specs:
        for example in spec.json_examples:
            call = parse_json_object(example)
            call_args = decode_json_object(call["args"])
            assert not validate_args_with_spec(spec, call_args), (spec.name, example)
            text = normalize_turn(decode_text_turn(example), snapshot=snapshot)
            native = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("id", spec.name, call_args),)), snapshot=snapshot)
            assert not text.protocol_error and not native.protocol_error, (spec.name, text.protocol_error, native.protocol_error)
            assert text.control_args == native.control_args
            assert [(c.name, c.args) for c in text.calls] == [(c.name, c.args) for c in native.calls]


def test_old_frozen_schema_stays_authoritative_after_registry_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "knowledge.read"})), task_kind="research")
    snapshot = build_turn_snapshot(session)
    original = tool_specs()["knowledge_read"]
    monkeypatch.setitem(tool_specs(), "knowledge_read", replace(original, required=("new_field",)))
    args: JsonObject = {"id": "previously-pinned-object"}
    for reply in (decode_text_turn(json.dumps({"tool": "knowledge_read", "args": args})),
                  AssistantTurn(tool_calls=(ProviderToolCall("frozen", "knowledge_read", args),))):
        assert not normalize_turn(reply, snapshot=snapshot).protocol_error


@pytest.mark.parametrize("mutation", ["missing", "unknown", "enum", "type", "bounds"])
def test_invalid_report_fields_rejected_identically(mutation: str) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "knowledge.read", "knowledge.write"}), strict_research=True))
    snapshot = build_turn_snapshot(session)
    spec = next(s for s in snapshot.frozen_specs if s.name == "submit_research_answer")
    args = decode_json_object(parse_json_object(spec.json_examples[0])["args"])
    if mutation == "missing":
        del args["state"]
    elif mutation == "unknown":
        args["invented"] = True
    elif mutation == "enum":
        args["state"] = "completed"
    elif mutation == "type":
        args["markdown"] = 7
    else:
        from codey.types.research_answer import MAX_ANSWER_CHARACTERS

        args["markdown"] = "x" * (MAX_ANSWER_CHARACTERS + 1)
    text = normalize_turn(decode_text_turn(json.dumps({"tool": spec.name, "args": args})), snapshot=snapshot)
    native = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("bad", spec.name, args),)), snapshot=snapshot)
    assert text.protocol_error == native.protocol_error and text.protocol_error


def test_text_declarations_expose_the_frozen_nested_constraints() -> None:
    from codey.research.tool_contract import done_spec_for_purpose
    from codey.toolchain.tool_prompt import json_contract_text
    from codey.toolchain.tool_spec import spec_for_tool
    from codey.utils.research_coverage import QUESTION_STATES

    base = spec_for_tool("done")
    assert base is not None
    spec = done_spec_for_purpose(base, "report")
    contract = json_contract_text((spec,))
    for state in QUESTION_STATES:
        assert '"' + state + '"' in contract
    assert '"state": string' not in contract
    assert 'pattern "^q[1-8]$"' in contract
    assert "items 1..8" in contract
    assert spec.parameters  # Rendering must leave the captured declaration unchanged.
