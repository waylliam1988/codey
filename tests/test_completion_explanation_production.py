"""Production prose assembly retains original receipts and strict final checks."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest
from playwright.sync_api import Route

from codey.app.api import run_submit_response
from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.app.task_submit import submit_task
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import evaluate
from codey.operations.completion_output import OutputBindingError, prepare_completion_output
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_preparation import prepare_kernel_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.output_requirement import (
    EXPLANATION_GUIDANCE,
    JSON_ANSWER_GUIDANCE,
    declaration,
    guidance,
    submission_fields,
)
from codey.policies.task_policy import TaskPolicy, build_task_policy
from codey.policies.tool_requirement import require_tool_success
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition
from codey.research.browser_search import BrowserSearchProvider
from codey.research.ledger import OpenedSource
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object
from tests.support.real_cdp import chromium_executable
from tools.completion_artifact_audit import rejection_audit, tool_receipts


def session_for(*bindings: str, strict: bool = False) -> TaskSession:
    return TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"}), strict_research=strict,
                                        required_checks=(declaration("json_explanation"), *bindings)), task_kind="chat")


def add_receipt(session: TaskSession, identity: str, value: str, *, ok: bool = True) -> None:
    call = ToolCall("read_file", {"path": "a.txt"}, identity)
    receipt = ToolResult(call, value, ok=ok, canonical={"value": {"text": value}})
    session._memory_results[identity] = receipt
    project_execution_record(session, identity, call, receipt, ok=ok)


@pytest.mark.parametrize("text", [
    '点击 "Load record"。\n\n路径 C:\\draft\\file，173 克。',
    '{"explanation":"this is still raw text","page_url":"model guess"}',
    '```json\n{"wrong":"still raw"}\n```',
    "",
])
def test_explicit_mode_preserves_summary_as_one_string_even_without_receipt_bindings(text: str) -> None:
    session = session_for()
    output = prepare_completion_output(session, text, CompletionContext())
    assert json.loads(output.text) == {"explanation": text}
    assert evaluate(session, output.text, observations=output.observations).complete
    assert session.last_done_text == ""


def test_missing_failed_and_conflicting_receipts_cannot_be_filled_by_model_claims() -> None:
    binding = require_tool_success("read_file", answer_pointer="/code", result_pointer="/text")
    session = session_for(binding)
    for stage in ("missing", "failed", "first", "conflict"):
        if stage != "missing":
            add_receipt(session, stage, "actual" if stage == "first" else "different", ok=stage != "failed")
        output = prepare_completion_output(session, 'code="actual"', CompletionContext())
        verdict = evaluate(session, output.text, observations=output.observations)
        assert verdict.complete is (stage == "first"), (stage, verdict)
        if stage == "first":
            assert json.loads(output.text) == {"code": "actual", "explanation": 'code="actual"'}
        else:
            assert "code" not in json.loads(output.text)


@pytest.mark.parametrize(("state", "reason"), [
    ("missing", "tool_execution_missing"), ("failed_parent", "tool_parent_missing"),
    ("bad_hash", "observation_receipt_invalid"), ("truncated", "observation_selector_unavailable"),
    ("partial", "observation_selector_unavailable"), ("complete", ""),
])
def test_browser_body_binding_uses_complete_original_receipt_before_format(state: str, reason: str) -> None:
    schema = '{"type":"object","properties":{"explanation":{"type":"string"},"body":{"type":"string"}},' \
             '"required":["explanation","body"]}'
    binding = require_tool_success("browser_read", via="codemode", answer_pointer="/body")
    session = session_for(binding)
    session.policy = replace(session.policy, required_checks=(declaration("json_explanation", schema), binding))
    text = "Verified page正文"
    url = "https://example.test/page"
    digest = hashlib.sha256(text.encode()).hexdigest()[:16]
    source = OpenedSource(url, url, "Page", "2026-11-11T00:00:00+00:00",
                          "tampered" if state == "bad_hash" else digest, truncated=state == "truncated")
    read = ToolResult(ToolCall("browser_read", {}, "read"), "decoy display", ok=True, canonical={
        "research_observation": {"source": source.to_dict(), "text": text, "pages": []},
        "read_passages": [{"start": 1 if state == "partial" else 0, "end": len(text)}],
    })
    if state != "missing":
        parent = ToolResult(ToolCall("codemode", {}, "parent"), "parent", ok=state != "failed_parent",
                            canonical={"child_effect_ids": ["read"]})
        for receipt in (read, parent):
            identity = receipt.call.call_id
            session._memory_results[identity] = receipt
            project_execution_record(session, identity, receipt.call, receipt, ok=receipt.ok)
    output = prepare_completion_output(session, "explanation", CompletionContext())
    verdict = evaluate(session, output.text, observations=output.observations)
    assert verdict.complete is (state == "complete"), verdict
    if reason:
        assert reason in verdict.followup and "output_format_invalid" not in verdict.followup
    else:
        assert json.loads(output.text)["body"] == text
    assert "value" not in read.canonical


@pytest.mark.parametrize("pointer", ["", "/explanation", "/explanation/child"])
def test_receipt_cannot_overwrite_declared_explanation_destination(pointer: str) -> None:
    session = session_for(require_tool_success("read_file", answer_pointer=pointer, result_pointer="/text"))
    with pytest.raises(OutputBindingError, match="overlaps"):
        prepare_completion_output(session, "original prose", CompletionContext())


def test_frozen_contract_and_restored_policy_keep_prose_semantics() -> None:
    binding = require_tool_success("read_file", answer_pointer="/code", result_pointer="/text")
    session = session_for(binding)
    add_receipt(session, "r1", "REAL-CODE")
    prepared = prepare_kernel_turn(session, user_task="Read and explain", context_text="", task_guidance="",
                                   native=False)
    assert EXPLANATION_GUIDANCE in prepared.prompt
    assert JSON_ANSWER_GUIDANCE not in prepared.prompt
    assert "supply only unbound JSON fields" not in prepared.prompt
    restored = restore_snapshot(snapshot_description(prepared.snapshot))
    assert restored.policy == session.policy
    assert restored.policy is not None
    fresh = TaskSession(policy=TaskPolicy.from_payload(restored.policy.to_payload()), task_kind="chat")
    for identity, receipt in session._memory_results.items():
        fresh._memory_results[identity] = receipt
        project_execution_record(fresh, identity, receipt.call, receipt, ok=receipt.ok)
    assert prepare_completion_output(fresh, 'quoted "text"\nnext', CompletionContext()) == prepare_completion_output(
        session, 'quoted "text"\nnext', CompletionContext())


def test_new_mode_uses_existing_submission_fields_and_rejects_strict_research() -> None:
    fields = submission_fields({"output_format": "json_explanation"})
    policy = build_task_policy(SimpleNamespace(project=None, **fields), task_kind="chat")
    assert policy.required_checks == (declaration("json_explanation"),)
    with pytest.raises(ValueError, match="strict Research"):
        build_task_policy(SimpleNamespace(project=None, strict_research=True, **fields), task_kind="research")
    with pytest.raises(OutputBindingError, match="strict Research"):
        prepare_completion_output(session_for(strict=True), "prose", CompletionContext())


def test_strict_research_rejects_prose_mode_before_reserving_application_state() -> None:
    def state() -> NoReturn:
        raise AssertionError("invalid output mode reached application state")

    with pytest.raises(ValueError, match="strict Research"):
        submit_task("session", None, "explain", 4, False, "local", get_state=state,
                    strict_research=True, output_format="json_explanation")


def test_api_forwards_production_output_mode_and_receipt_bindings() -> None:
    binding = require_tool_success("read_file", answer_pointer="/code", result_pointer="/text")
    submitted: dict[str, object] = {}

    def submit(*_args: object, **kwargs: object) -> str:
        submitted.update(kwargs)
        return "accepted"

    status, _ = run_submit_response({"task": "read and explain", "output_format": "json_explanation",
                                     "required_tools": [binding]}, submit)
    assert status == 200
    assert submitted["output_format"] == "json_explanation"
    assert submitted["required_tools"] == (binding,)


@pytest.mark.parametrize("schema", [
    '{"type":"array","items":{"type":"string"}}',
    '{"type":"object","properties":{"explanation":{"type":"integer"}},"required":["explanation"]}',
    '{"type":"object","properties":{"explanation":{"type":"string"}}}',
])
def test_invalid_prose_destination_schema_is_rejected(schema: str) -> None:
    with pytest.raises(ValueError, match="required string explanation"):
        declaration("json_explanation", schema)


def test_schema_rejection_requests_prose_without_telling_model_to_encode_inner_json() -> None:
    schema = '{"type":"object","properties":{"explanation":{"type":"string","minLength":5}},"required":["explanation"]}'
    session = session_for()
    session.policy = replace(session.policy, required_checks=(declaration("json_explanation", schema),))
    output = prepare_completion_output(session, "x", CompletionContext())
    verdict = evaluate(session, output.text, observations=output.observations)
    assert not verdict.complete
    assert EXPLANATION_GUIDANCE in verdict.followup
    assert JSON_ANSWER_GUIDANCE not in verdict.followup
    assert "application's final output" in guidance(session.policy.required_checks)


class ScriptedProvider(ApiProvider):
    def __init__(self, replies: list[JsonObject], *, native: bool) -> None:
        super().__init__("http://localhost:9/v1", "fixture", native_tools=native)
        self.replies = iter(replies)
        self.calls = 0

    def _generate(self, messages: list[dict[str, object]], tools: list[ProviderToolDefinition] | None = None,
                  *, timeout: float | None = None, checkpoints: list[dict[str, object]] | None = None,
                  tool_call_json: bool = False, tool_call_schema: dict[str, object] | None = None,
                  tool_call_contract: str = "", tool_call_observations: str = "",
                  tool_call_history_groups: int = 0, output_token_limit: int = 0,
                  require_tool_call: bool = False) -> dict[str, object]:
        self.calls += 1
        reply = next(self.replies)
        if self.native_tools:
            message: dict[str, object] = {"role": "assistant", "content": "", "tool_calls": [{
                "id": f"call-{self.calls}", "type": "function", "function": {
                    "name": reply["tool"], "arguments": json.dumps(reply["args"])}}]}
        else:
            message = {"role": "assistant", "content": json.dumps(reply), "tool_calls": []}
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if self.native_tools else "stop"}]}


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize(("state", "field"), [
    ("complete", ""), ("missing", "amount"), ("partial", "unit"), ("wrong_type", "amount"),
    ("truncated", "amount"), ("stale", "amount"), ("failed_parent", "amount"),
])
def test_headless_binds_declared_business_fields_from_current_browser_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, field: str,
) -> None:
    url = "https://example.com/business-record"
    token = "PAGE_TOKEN_37"
    record: JsonObject = {"amount": 173, "unit": "克", "code": "QUARTZ_851"}
    later_record: JsonObject = {"amount": 227, "unit": "克", "code": "QUARTZ_906"}
    initial = dict(record)
    if state == "partial":
        del initial["unit"]
    if state == "wrong_type":
        initial["amount"] = "173"
    if state == "truncated":
        initial["padding"] = "X" * 40_000
    browser = BrowserSearchProvider(headless=True, browser_path=chromium_executable())
    navigation_count = 0

    def fixture_route(route: Route) -> None:
        nonlocal navigation_count
        navigation_count += 1
        value = initial if navigation_count == 1 else later_record
        route.fulfill(status=200, content_type="text/html", body=(
            "<html><body>Business record<button onclick='window.__record=window.__nextRecord'>Refresh record</button><script>"
            f"window.__record={json.dumps(value)};window.__contractToken={json.dumps(token)};"
            f"window.__nextRecord={json.dumps(later_record)};"
            "</script></body></html>"))

    monkeypatch.setattr(browser, "_guard_request", fixture_route)
    monkeypatch.setattr("codey.research.search_factory.default_research_search_provider", lambda: browser)
    monkeypatch.setattr("codey.operations.task_phases.dispatch.default_research_search_provider", lambda: browser)
    monkeypatch.setattr("codey.app.provider_services.provider_availability", lambda: {"local": True})

    nav = "unwrap(await tools.browser_navigate({url:inputs.url}));"
    meta = ("unwrap(await tools.browser_tab_list({}));"
            "unwrap(await tools.browser_eval({code:'window.__contractToken'}));")
    read = "return unwrap(await tools.browser_eval({code:'window.__record'}));"
    prose = '按页面读取记录。模型草稿 {"amount":999}。'
    done: JsonObject = {"tool": "done", "args": {"summary": prose}}
    code = nav + meta + ("return null;" if state in {"missing", "failed_parent"} else read)
    replies: list[JsonObject] = [{"tool": "codemode", "args": {"code": code, "inputs": {"url": url}}}]
    if state == "failed_parent":
        replies.append({"tool": "codemode", "args": {
            "code": "unwrap(await tools.browser_eval({code:'window.__record'}));throw Error('after read');"}})
    if state == "stale":
        replies.append({"tool": "codemode", "args": {"code": nav + "return null;", "inputs": {"url": url}}})
    replies.append(done)
    if state != "complete":
        prefix = ("const s=unwrap(await tools.browser_snapshot({}));"
                  "unwrap(await tools.browser_click({ref:s.elements.find(e=>e.name==='Refresh record').ref}));"
                  if state in {"partial", "wrong_type", "truncated"} else "")
        replies.extend([{"tool": "codemode", "args": {"code": prefix + read, "inputs": {"url": url}}}, done])
    provider = ScriptedProvider(replies, native=False)

    def connect(_provider_id: str, **_kwargs: object) -> ScriptedProvider:
        return provider

    events: list[JsonObject] = []

    def observe(value: dict[str, object]) -> None:
        events.append(decode_json_object(value))

    schema = {"type": "object", "properties": {
        key: {"type": "integer"} if key == "amount" else {"type": "string", "minLength": 1}
        for key in ("explanation", "page_url", "token", "amount", "unit", "code")},
        "required": ["explanation", "page_url", "token", "amount", "unit", "code"], "additionalProperties": False}
    bindings = (
        require_tool_success("browser_tab_list", via="codemode", answer_pointer="/page_url", result_pointer="/page_url"),
        require_tool_success("browser_eval", via="codemode", arguments={"code": "window.__contractToken"},
                             answer_pointer="/token", result_pointer="/value"),
        *(require_tool_success("browser_eval", via="codemode", arguments={"code": "window.__record"},
                               answer_pointer=f"/{key}", result_pointer=f"/value/{key}") for key in record),
    )
    try:
        result = run_headless(HeadlessRequest(None, "Read and explain the browser's business record.", intent="chat",
            provider_id="local", max_turns=6, state_home=tmp_path / "state", research_store_root=tmp_path / "knowledge",
            requested_capabilities=("web.read", "browser.observe", "browser.interact", "browser.effect"),
            output_format="json_explanation", output_schema=json.dumps(schema), required_tools=bindings),
            emit_jsonl=observe, connect_provider=connect)
    finally:
        browser.close()
        provider.close()
    terminal = next(row for row in reversed(events) if row["type"] == "task_done")
    assert result.stop_reason == "done", terminal
    expected = record if state in {"complete", "missing", "failed_parent"} else later_record
    assert json.loads(str(terminal["summary"])) == {"page_url": url, "token": token, "explanation": prose, **expected}
    assert provider.calls == len(replies)
    receipts = tool_receipts(tmp_path / "state")
    read_receipts = [r.result for r in receipts if r.result.ok and r.result.call.args.get("code") == "window.__record"]
    assert len(read_receipts) == (1 if state in {"complete", "missing"} else 2)
    documents = [decode_json_object(r.audit["browser_document"]) for r in read_receipts]
    assert all(document["url"] == url for document in documents)
    if state == "stale":
        assert documents[0]["loader_id"] != documents[1]["loader_id"]
    if state in {"partial", "wrong_type", "truncated"}:
        assert documents[0] == documents[1]
    rejection = rejection_audit(tmp_path / "state")["first_rejection"]
    if field:
        reason = str(decode_json_object(rejection)["reason"])
        assert f"'/{field}'" in reason and f"'/value/{field}'" in reason
        assert "output_format_invalid" not in reason
    else:
        assert rejection is None


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("mode", ["chat", "hybrid"])
@pytest.mark.parametrize("prose", ['\n  exact "explanation"  \n', '{"source_gaps":"business data"}', ""])
def test_headless_production_publishes_prose_and_receipt_fields_without_provider_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool, mode: str, prose: str,
) -> None:
    def availability(*_args: object, **_kwargs: object) -> dict[str, bool]:
        return {"local": True}

    monkeypatch.setattr("codey.app.provider_services.provider_availability", availability)
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("REAL-CODE\n", encoding="utf-8")
    done_args: JsonObject = {"summary": prose}
    if mode == "chat":
        done_args["source_gaps"] = [{"reason": "None", "result_ref": "invented"}]
    provider = ScriptedProvider([
        {"tool": "codemode", "args": {"code": "return unwrap(await tools.read_file({path:'a.txt'}));"}},
        {"tool": "done", "args": done_args},
    ], native=native)
    events: list[JsonObject] = []

    def observe(value: dict[str, object]) -> None:
        events.append(decode_json_object(value))

    def connect(_provider_id: str, **_kwargs: object) -> ScriptedProvider:
        return provider

    schema = {"type": "object", "properties": {"code": {"type": "string"}, "explanation": {"type": "string"}},
              "required": ["code", "explanation"], "additionalProperties": False}
    result = run_headless(HeadlessRequest(project, "Read a.txt and explain.", provider_id="local", intent=mode,
        project_changes_required=False, max_turns=3, state_home=tmp_path / "state",
        research_store_root=tmp_path / "knowledge", output_format="json_explanation", output_schema=json.dumps(schema),
        required_tools=(require_tool_success("read_file", via="codemode", arguments={"path": "a.txt"},
                                            answer_pointer="/code", result_pointer="/text"),)),
        emit_jsonl=observe, connect_provider=connect)
    terminal = next(row for row in reversed(events) if row["type"] == "task_done")
    assert result.stop_reason == "done", terminal
    assert json.loads(str(terminal["summary"])) == {"code": "REAL-CODE\n", "explanation": prose}
    assert provider.calls == 2
    assert sum(row["type"] == "tool" and row.get("tool_name") == "read_file" for row in events) == 1


@pytest.mark.parametrize("native", [False, True])
def test_optional_report_proposal_cannot_choose_repair_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    from codey.operations import kernel_prompt

    modes: list[bool] = []
    render = kernel_prompt._completion_followup

    def capture(feedback: str, *, native: bool, structured_report: bool = False,
                whole_submission: bool = False) -> str:
        modes.append(structured_report)
        return render(feedback, native=native, structured_report=structured_report, whole_submission=whole_submission)

    monkeypatch.setattr(kernel_prompt, "_completion_followup", capture)
    monkeypatch.setattr("codey.app.provider_services.provider_availability", lambda: {"local": True})
    provider = ScriptedProvider([
        {"tool": "done", "args": {"summary": "x", "report_sections": {"evidence": ["Unsupported report"],
            "counter": [], "coverage": [{"question_ref": "q1", "state": "not_investigated", "answer": "unknown",
                                       "findings": [], "investigations": []}]}}},
        {"tool": "done", "args": {"summary": "A sufficiently complete explanation."}},
    ], native=native)

    def connect(_provider_id: str, **_kwargs: object) -> ScriptedProvider:
        return provider

    schema = {"type": "object", "properties": {"explanation": {"type": "string", "minLength": 10}},
              "required": ["explanation"], "additionalProperties": False}

    def observe(_row: dict[str, object]) -> None:
        pass

    result = run_headless(HeadlessRequest(None, "Explain the result.", provider_id="local", intent="chat",
        max_turns=3, state_home=tmp_path / "state", research_store_root=tmp_path / "knowledge",
        output_format="json_explanation", output_schema=json.dumps(schema)), connect_provider=connect, emit_jsonl=observe)
    assert result.stop_reason == "done"
    assert modes == [False]
