"""Suffix-only completion repair stays behind frozen schemas and final checks."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import evaluate
from codey.operations.completion_output import prepare_completion_output
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.output_requirement import EXPLANATION_DONE_EXAMPLE, declaration
from codey.policies.task_policy import TaskPolicy
from codey.protocols.json_repair import close_json_tail
from codey.protocols.text_turn import DecodedTextTurn, TextError, decode_text_turn
from codey.providers.api_chat import decode_turn
from codey.providers.api_responses import decode_exchange
from codey.providers.base import AssistantTurn, TurnFinish
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, parse_json_object
from tests.test_explanation_delivery_contract import session_for
from tests.test_research_submission_handoff_production import NativeExperimentProvider
from tools.completion_artifact_audit import artifacts, tool_receipts
from tools.completion_explanation_ab import ExperimentProvider, Target


def native_message(tool: str, arguments: str, identity: str = "done-1") -> JsonObject:
    return {"id": identity, "type": "function", "function": {"name": tool, "arguments": arguments}}


def native_reply(tool: str, arguments: str, transport: str) -> AssistantTurn:
    if transport == "chat":
        turn, stored = decode_turn({"tool_calls": [native_message(tool, arguments)]})
        assert stored is None
    else:
        turn, stored_items = decode_exchange({"status": "completed", "output": [
            {"type": "function_call", "call_id": "done-1", "name": tool, "arguments": arguments}]},
            text_only=False, text_decoder=None)
        assert not stored_items
    assert turn.finish is TurnFinish.PROTOCOL_ERROR
    return turn


@pytest.mark.parametrize("source", [
    '{"a":["完整文字。"', '{"a":["escaped \\\"quote\\\" and } ]"]', '  {"a":{"b":"字面值"} \n',
])
def test_pure_tail_closure_only_appends_matching_container_delimiters(source: str) -> None:
    closed = close_json_tail(source)
    assert closed is not None and closed.original == source
    assert closed.candidate == source + closed.suffix and set(closed.suffix) <= {"}", "]"}
    parse_json_object(closed.candidate)


@pytest.mark.parametrize("source", [
    '', '{"a":"unfinished', '{"a":"unfinished\\', '{"a":123', '{"a":1e', '{"a":-',
    '{"a":', '{"a"', '{"a":["first","second",', '{"a":["first" "second"]',
    '{"a":["first"}', '{"a":["first"],"a":["second"]', '{"a":null,"b":NaN',
    '{"a":["first"]}', '{"a":["first"]} {"a":["second"]', 'prose {"a":["first"]',
    '```json\n{"a":["first"]', '[{"a":["first"]',
])
def test_ambiguous_or_already_complete_json_has_no_tail_candidate(source: str) -> None:
    assert close_json_tail(source) is None


@pytest.mark.parametrize("transport", ["text", "text_wrapped", "chat", "responses"])
@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("missing", [1, 2])
def test_only_frozen_paragraph_completion_adopts_a_single_tail_candidate(transport: str, restored: bool, missing: int) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control"}), required_checks=(declaration("json_explanation"),)), task_kind="chat")
    native = transport in {"chat", "responses"}
    snapshot = build_turn_snapshot(session, native=native)
    if restored:
        snapshot = restore_snapshot(snapshot_description(snapshot))
    arguments = json.dumps(parse_json_object(EXPLANATION_DONE_EXAMPLE)["args"], ensure_ascii=False, separators=(",", ":"))
    raw = (arguments if native else EXPLANATION_DONE_EXAMPLE)[:-missing]
    reply: DecodedTextTurn | AssistantTurn
    if native:
        reply = native_reply("done", raw, transport)
    elif transport == "text_wrapped":
        reply = AssistantTurn(text=raw, raw={"text_protocol": True})
    else:
        reply = decode_text_turn(raw)
        assert isinstance(reply, TextError)
    plan = normalize_turn(reply, snapshot=snapshot)
    assert not plan.protocol_error and not plan.calls and plan.control is not None
    assert plan.control.body == "First paragraph.\n\nSecond paragraph."
    assert plan.syntax_closure is not None and plan.syntax_closure.candidate == raw + plan.syntax_closure.suffix
    assert plan.arg_repair_counts == {"syntax_closed": 1}


@pytest.mark.parametrize("tool", ["codemode", "edit", "browser_click", "browser_navigate", "ask_user", "read_tool_result"])
@pytest.mark.parametrize("transport", ["text", "chat", "responses"])
def test_tail_closure_never_promotes_other_tools(tool: str, transport: str) -> None:
    session = session_for("FACT_928")
    snapshot = build_turn_snapshot(session, native=transport != "text")
    raw = json.dumps({"paragraphs": ["FACT_928"]})[:-1]
    reply = (decode_text_turn('{"tool":' + json.dumps(tool) + ',"args":' + raw) if transport == "text" else
             native_reply(tool, raw, transport))
    plan = normalize_turn(reply, snapshot=snapshot)
    assert plan.protocol_error and not plan.calls and plan.syntax_closure is None


@pytest.mark.parametrize("arguments", [
    '{"summary":"old prose"', '{"paragraphs":[]', '{"paragraphs":[17]',
    '{"paragraphs":["line\\nbreak"]', '{"paragraphs":["ok"],"report_sections":{}',
    '{"paragraphs":["ok"],"execution_claims":{}', '{"paragraphs":["incomplete',
    '{"paragraphs":["one" "two"]', '{"paragraphs":["one"],"missing":',
])
@pytest.mark.parametrize("transport", ["text", "chat", "responses"])
def test_tail_candidate_cannot_bypass_frozen_argument_validation(arguments: str, transport: str) -> None:
    session = session_for("FACT_928")
    snapshot = build_turn_snapshot(session, native=transport != "text")
    reply = (decode_text_turn('{"tool":"done","args":' + arguments) if transport == "text" else
             native_reply("done", arguments, transport))
    plan = normalize_turn(reply, snapshot=snapshot)
    assert plan.protocol_error and not plan.calls and plan.syntax_closure is None


def test_native_batch_and_unfrozen_or_ordinary_completion_are_not_repaired() -> None:
    malformed = native_message("done", '{"paragraphs":["FACT_928"]')
    turn, stored = decode_turn({"tool_calls": [malformed, native_message("edit", '{"path":"a.txt"}', "write-1")]})
    assert stored is None
    snapshot = build_turn_snapshot(session_for("FACT_928"), native=True)
    assert normalize_turn(turn, snapshot=snapshot).protocol_error
    ordinary = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control"})), task_kind="chat"))
    raw = decode_text_turn(EXPLANATION_DONE_EXAMPLE[:-1])
    for captured in (None, ordinary):
        plan = normalize_turn(raw, snapshot=captured)
        assert plan.protocol_error and plan.syntax_closure is None


@pytest.mark.parametrize("fact_present", [False, True])
def test_syntax_closure_still_requires_the_actual_completion_checks(fact_present: bool) -> None:
    session = session_for("FACT_928")
    fact = "FACT_928" if fact_present else "Wrong value"
    raw = '{"tool":"done","args":{"paragraphs":[' + json.dumps(fact) + ']'
    plan = normalize_turn(decode_text_turn(raw), snapshot=build_turn_snapshot(session))
    assert plan.syntax_closure is not None and plan.control is not None
    output = prepare_completion_output(session, plan.control.body, CompletionContext())
    verdict = evaluate(session, output.text, observations=output.observations)
    assert verdict.complete is fact_present
    if not fact_present:
        assert "answer_text_missing" in verdict.followup


@pytest.mark.usefixtures("scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("native", [False, True])
def test_production_records_original_and_closed_candidate_before_final_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    directory = tmp_path / "run"
    directory.mkdir()
    paragraphs: tuple[list[JsonValue], list[JsonValue]] = (
        ["Wrong first paragraph.", "Wrong second paragraph."], ["First paragraph.", "Second paragraph."])
    originals: list[str] = []

    def scripted(_url: str, payload: dict[str, object], _headers: dict[str, str], **kwargs: object) -> JsonObject:
        observer = kwargs["observe"]
        assert callable(observer)
        observer(attempt=1, data=json.dumps(payload).encode(), phase="request", response_bytes=0, seconds=0.0)
        args: JsonObject = {"paragraphs": paragraphs[len(originals)]}
        raw = json.dumps(args if native else {"tool": "done", "args": args}, ensure_ascii=False)[:-1]
        originals.append(raw)
        message: JsonObject = {"role": "assistant", "content": raw}
        if native:
            message.update(content=None, tool_calls=[native_message("done", raw, "done-" + str(len(originals)))])
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if native else "stop"}]}

    monkeypatch.setattr("codey.providers.api_generation.generate", scripted)
    provider_type = NativeExperimentProvider if native else ExperimentProvider
    provider = provider_type(Target("http://scripted.test/v1", "fixture", 123456789, 10), directory, fixed_acquisition=False)

    def connect(_identity: str, **_kwargs: object) -> ExperimentProvider:
        return provider

    events: list[dict[str, object]] = []
    expected = "First paragraph.\n\nSecond paragraph."
    schema: JsonObject = {"type": "object", "properties": {"explanation": {"type": "string", "enum": [expected]}},
                          "required": ["explanation"], "additionalProperties": False}
    result = run_headless(HeadlessRequest(project=None, task="Explain the verified result in two paragraphs.", intent="chat",
        provider_id="local", max_turns=3, state_home=directory / "state", output_format="json_explanation",
        output_schema=json.dumps(schema)), emit_jsonl=events.append, connect_provider=connect)
    assert result.stop_reason == "done", events
    assert len(originals) == 2 and not tool_receipts(directory / "state")
    records = artifacts(directory / "state")
    by_handle = dict(records)
    audits = [body for _, body in records if body.get("kind") == "syntax_closed"]
    assert len(audits) == 2
    for original, audit in zip(originals, audits, strict=True):
        source = decode_json_object(by_handle[str(audit["source_response_ref"])]["payload"])
        if native:
            raw = decode_json_object(source["raw"])["malformed_native_calls"]
            assert isinstance(raw, list) and len(raw) == 1
            assert decode_json_object(decode_json_object(raw[0])["function"])["arguments"] == original
        else:
            assert source["text"] == original
        assert audit["candidate"] == original + str(audit["suffix"])
        assert audit["transport"] == ("native_arguments" if native else "text")
        parse_json_object(str(audit["candidate"]))
