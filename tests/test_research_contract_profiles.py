"""Frozen declarations, examples and both protocols share one structural contract."""

import json
from dataclasses import replace

import pytest

from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.toolchain.tool_spec import tool_specs, validate_args_with_spec


@pytest.mark.parametrize("purpose", ["ordinary", "evidence", "report"])
def test_every_visible_example_passes_frozen_spec_and_both_protocols(purpose):
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "knowledge.read", "knowledge.write", "knowledge.link"}),
                                   strict_research=purpose == "report"),
                          task_kind="evidence_followup" if purpose == "evidence" else "research")
    session.research_questions = ("Main question",)
    snapshot = build_turn_snapshot(session)
    for spec in snapshot.frozen_specs:
        for example in spec.json_examples:
            call = json.loads(example)
            assert not validate_args_with_spec(spec, call["args"]), (spec.name, example)
            text = normalize_turn(decode_text_turn(example), snapshot=snapshot)
            native = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("id", spec.name, call["args"]),)), snapshot=snapshot)
            assert not text.protocol_error and not native.protocol_error, (spec.name, text.protocol_error, native.protocol_error)
            assert text.control_args == native.control_args
            assert [(c.name, c.args) for c in text.calls] == [(c.name, c.args) for c in native.calls]


def test_old_frozen_schema_stays_authoritative_after_registry_changes(monkeypatch):
    session = TaskSession(TaskPolicy(frozenset({"control", "knowledge.read"})), task_kind="research")
    snapshot = build_turn_snapshot(session)
    original = tool_specs()["knowledge_read"]
    monkeypatch.setitem(tool_specs(), "knowledge_read", replace(original, required=("new_field",)))
    args = {"id": "previously-pinned-object"}
    for reply in (decode_text_turn(json.dumps({"tool": "knowledge_read", "args": args})),
                  AssistantTurn(tool_calls=(ProviderToolCall("frozen", "knowledge_read", args),))):
        assert not normalize_turn(reply, snapshot=snapshot).protocol_error


@pytest.mark.parametrize("mutation", ["missing", "unknown", "enum", "type", "bounds"])
def test_invalid_report_fields_rejected_identically(mutation):
    session = TaskSession(TaskPolicy(frozenset({"control", "knowledge.read"}), strict_research=True))
    snapshot = build_turn_snapshot(session)
    done = next(s for s in snapshot.frozen_specs if s.name == "done")
    args = json.loads(done.json_examples[0])["args"]
    row = args["report_sections"]["coverage"][0]
    if mutation == "missing":
        del row["investigations"]
    elif mutation == "unknown":
        row["invented"] = True
    elif mutation == "enum":
        row["state"] = "completed"
    elif mutation == "type":
        row["findings"] = "f1"
    else:
        row["answer"] = "x" * 4001
    text = normalize_turn(decode_text_turn(json.dumps({"tool": "done", "args": args})), snapshot=snapshot)
    native = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("bad", "done", args),)), snapshot=snapshot)
    assert text.protocol_error == native.protocol_error and text.protocol_error
