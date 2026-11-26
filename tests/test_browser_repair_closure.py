"""Declared types, staged waits and durable answer-only repair use production paths."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from codey.codemode.catalog import build_catalog_snapshot, render_declarations
from codey.codemode.kernel import build_execute_turn_executor
from codey.codemode.shape_repair import readonly_settled, shape_diagnostic
from codey.codemode.types import ToolCatalogEntry
from codey.memory.candidates import CANDIDATE_FIELD
from codey.operations.browser.browser_followups import click_followup_plan
from codey.operations.browser.catalog import snapshot_catalog
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import evaluate
from codey.operations.completion_output import explanation_text, prepare_completion_output
from codey.operations.explanation_repair import repair_prompt as explanation_repair_prompt
from codey.operations.file_field_binding import bound_content, file_rules, validate_content
from codey.operations.kernel.kernel_execution import execute_turn
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.kernel.turn_presentation import refresh_snapshot_contract
from codey.operations.kernel_contracts import RetryDecision, ToolExecutor
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_preparation import prepare_kernel_turn
from codey.operations.kernel_prompt import _completion_followup, _repair_prompt, kernel_prompt_for_session
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.operations.tool_result_reader import ReceiptReader, read_tool_result
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.output_requirement import EXPLANATION_DONE_EXAMPLE, declaration
from codey.policies.task_policy import TaskPolicy, build_task_policy
from codey.policies.tool_requirement import (
    ToolObservation,
    parse_tool_success_check,
    require_browser_file_field,
    require_browser_read_field,
    require_tool_success,
)
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall, TurnFinish
from codey.research.source_document import OpenedSource
from codey.research.source_observation import source_snapshot
from codey.runtime.core.models import ToolCall, ToolResult
from codey.task.web_intent import explicit_browser_checks
from codey.toolchain.spec_model import schema_for_spec
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters, spec_for_tool
from codey.types.json_value import JsonObject, JsonValue, canonical_json_object, decode_json_object, parse_json_object
from tests.test_explanation_delivery_contract import session_for
from tests.test_receipt_bound_edit_surface_probe import install_http
from tests.test_research_submission_handoff_production import NativeExperimentProvider
from tests.test_research_submission_handoff_production import install_http as install_protocol_http
from tests.test_research_submission_handoff_production import requests as provider_requests
from tools.browser_prerequisite_ab import LOAD, WAIT, code_action
from tools.browser_production_live_gate import run_case
from tools.completion_artifact_audit import artifacts, tool_receipts
from tools.completion_explanation_ab import Target


def record(session: TaskSession, name: str, args: JsonObject, document: JsonObject,
           *, ok: bool = True, body: str = "") -> str:
    ref = "receipt-" + str(len(session.executed))
    canonical: JsonObject = {"value": {"matched": ok, "page_url": document["url"]}}
    if body:
        source = OpenedSource(str(document["url"]), str(document["url"]), "Record", "2026-11-15T00:00:00Z",
                              hashlib.sha256(body.encode()).hexdigest()[:16])
        canonical = {"research_observation": source_snapshot(source, body, {}),
                     "read_passages": [{"start": 0, "end": len(body)}]}
    result = ToolResult(ToolCall(name, args, ref), body or "receipt", ok=ok, canonical=canonical,
                        audit={"browser_document": document})
    session._memory_results[ref] = result
    project_execution_record(session, ref, result.call, result, ok=ok)
    return ref


@pytest.mark.parametrize(("literal", "expected"), [
    ('“鱼丸”后，再调用 browser_read', "鱼丸"), ('"Ready" 后，再调用 browser_read', "Ready"),
    ("`Ready` 后，再调用 browser_read", "Ready"), ("'Ready'，再调用 browser_read", "Ready"),
    ("结算完成后，再调用 browser_read", "结算完成"), ('“未闭合，调用 browser_read', ""),
])
def test_wait_literals_do_not_capture_following_instructions(literal: str, expected: str) -> None:
    checks = explicit_browser_checks("调用 browser_wait 等待出现" + literal + "。")
    rule = parse_tool_success_check(checks[0])
    assert rule is not None
    assert parse_json_object(rule.arguments_json) == ({"condition": "text", "value": expected} if expected else {})
    plan = click_followup_plan(checks, {"browser_wait", "browser_read"})
    assert ("result" in plan.plans) is bool(expected)


@pytest.mark.parametrize("wrong", ["", "early", "swapped", "other_document", "failed"])
def test_wait_stages_preserve_prior_success_and_reject_wrong_order(wrong: str) -> None:
    task = "点击准备，调用 browser_wait 等待出现“就绪”后，点击提交，调用 browser_wait 等待出现`完成`，最后调用 browser_read。"
    policy = build_task_policy(SimpleNamespace(task=task), task_kind="hybrid")
    rules = [rule for check in policy.required_checks if (rule := parse_tool_success_check(check)) is not None]
    assert [rule.stage for rule in rules] == [1, 2, 2]
    session = TaskSession(policy, task_kind="hybrid")
    document: JsonObject = {"url": "https://example.com/phases", "document_id": "current"}
    if wrong == "early":
        record(session, "browser_wait", {"condition": "text", "value": "就绪"}, document)
    record(session, "browser_click", {}, document)
    if wrong != "early":
        record(session, "browser_wait", {"condition": "text", "value": "完成" if wrong == "swapped" else "就绪"}, document)
    record(session, "browser_click", {}, document)
    record(session, "browser_wait", {"condition": "text", "value": "就绪" if wrong == "swapped" else "完成"},
           {**document, "document_id": "old"} if wrong == "other_document" else document, ok=wrong != "failed")
    record(session, "browser_read", {}, document)
    outcomes = [rule.observe(session.executed, results=session._memory_results, browser_document=document).evaluation.passed
                for rule in rules]
    assert all(outcomes) is (not wrong), outcomes
    assert "result" not in click_followup_plan(policy.required_checks, {"browser_wait", "browser_read"}).plans
    first = click_followup_plan(policy.required_checks, {"browser_wait", "browser_read"}, stage=1)
    second = click_followup_plan(policy.required_checks, {"browser_wait", "browser_read"}, stage=2)
    assert '"value": "就绪"' in json.dumps(first.plans, ensure_ascii=False) and '"value": "完成"' not in json.dumps(first.plans, ensure_ascii=False)
    assert '"value": "完成"' in json.dumps(second.plans, ensure_ascii=False) and '"value": "就绪"' not in json.dumps(second.plans, ensure_ascii=False)


def test_generic_wait_requirement_uses_the_concrete_declared_predicate() -> None:
    checks = (require_tool_success("browser_wait"), *explicit_browser_checks("调用 browser_wait 等待出现`Ready`。"))
    plan = click_followup_plan(checks, {"browser_wait", "browser_read"})
    assert "result" in plan.plans
    assert "Ready" in json.dumps(plan.plans)
    unresolved = click_followup_plan((require_tool_success("browser_wait"),), {"browser_wait", "browser_read"})
    assert "result" not in unresolved.plans


@pytest.mark.parametrize(("value", "kind", "encoded"), [
    ("173", "number", "173"), ("-0.1250", "number", "-0.1250"),
    ("12345678901234567890.1234567890123456789", "number", "12345678901234567890.1234567890123456789"),
    ("1.25e-12", "number", "1.25e-12"), ("000173", "string", '"000173"'),
    ("1e999999999999999999999999", "number", "1e999999999999999999999999"),
    ("173", "integer", "173"),
])
def test_file_contract_copies_exact_declared_types_and_checks_actual_file(
    tmp_path: Path, value: str, kind: str, encoded: str,
) -> None:
    url = "https://example.com/financial-record"
    binding = require_browser_file_field(url=url, prefix="Amount: ", suffix=" units.", path="record.json",
                                         pointer="/amount", value_type=kind)
    policy = build_task_policy(SimpleNamespace(project=str(tmp_path), required_tools=(binding,)), task_kind="hybrid")
    session = TaskSession(policy, task_kind="hybrid", project=str(tmp_path))
    document: JsonObject = {"url": url, "document_id": "current"}
    record(session, "browser_read", {}, document, body=f"Amount: {value} units.")
    original = '{ "amount":0,"other":9007199254740993.123456789 }\n'
    copied = bound_content(session, "record.json", original, document)
    assert copied == original.replace('"amount":0', '"amount":' + encoded)
    assert not validate_content(policy.required_checks, "record.json", copied, session.executed, session._memory_results, document)
    wrong = '{"amount":' + (json.dumps(value) if kind != "string" else "173") + "}"
    assert validate_content(policy.required_checks, "record.json", wrong, session.executed, session._memory_results, document)
    assert file_rules(policy.required_checks)[0].declaration == binding
    (tmp_path / "record.json").write_text(wrong, encoding="utf-8")
    output = prepare_completion_output(session, "done", CompletionContext(browser_document=document))
    verdict = evaluate(session, output.text, context=CompletionContext(project=str(tmp_path), browser_document=document), observations=output.observations)
    assert not verdict.complete and "file_field_mismatch" in verdict.followup


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("paragraphs", [["第一段。", "第二段。"], ["single paragraph"], ["line\nbreak"], [], [17]])
def test_paragraph_contract_is_structured_and_restorable(paragraphs: list[object], native: bool) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control"}), required_checks=(declaration("json_explanation"),)), task_kind="chat")
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session, native=native)))
    args = decode_json_object({"paragraphs": paragraphs})
    reply = (AssistantTurn(tool_calls=(ProviderToolCall("done-1", "done", args),)) if native else
             decode_text_turn(json.dumps(dict[str, object](tool="done", args=args))))
    plan = normalize_turn(reply, snapshot=snapshot)
    valid = bool(paragraphs) and all(isinstance(part, str) and "\n" not in part for part in paragraphs)
    assert bool(plan.protocol_error) is (not valid)
    if valid:
        assert plan.control is not None and plan.control.body == "\n\n".join(str(part) for part in paragraphs)


@pytest.mark.parametrize("kind", ["chat", "hybrid"])
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("extra", ["summary", "report_sections", "open_questions", "knowledge_refs", "execution_claims", "source_gaps"])
def test_explanation_interface_only_exposes_paragraphs(kind: str, native: bool, extra: str) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control"}), required_checks=(declaration("json_explanation"),)),
                          task_kind=kind)
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session, native=native)))
    done = next(spec for spec in snapshot.model_specs if spec.name == "done")
    assert set(dict(done.parameters)) == {"paragraphs"}
    assert done.required == ("paragraphs",)
    assert schema_for_spec(done)["additionalProperties"] is False
    assert "report_sections.coverage" not in snapshot.contract_text and "open_questions" not in snapshot.contract_text
    assert len(done.json_examples) == 1
    assert done.json_examples[0] == '{"tool":"done","args":{"paragraphs":["First paragraph.","Second paragraph."]}}'
    assert "exactly one property: paragraphs" in done.description
    submitted: JsonObject = {"paragraphs": ["Business explanation."], extra: None}
    reply = (AssistantTurn(tool_calls=(ProviderToolCall("done-1", "done", submitted),)) if native else
             decode_text_turn(json.dumps(dict[str, object](tool="done", args=submitted))))
    assert f"unexpected arg '{extra}'" in normalize_turn(reply, snapshot=snapshot).protocol_error
    legacy: JsonObject = {"summary": "Earlier prose form."}
    legacy_reply = (AssistantTurn(tool_calls=(ProviderToolCall("done-2", "done", legacy),)) if native else
                    decode_text_turn(json.dumps(dict[str, object](tool="done", args=legacy))))
    assert normalize_turn(legacy_reply, snapshot=snapshot).protocol_error
    direct = AssistantTurn(text="Direct prose") if native else decode_text_turn("Direct prose")
    assert "done.paragraphs" in normalize_turn(direct, snapshot=snapshot).protocol_error


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("request_kind", ["unknown", "malformed_unknown", "missing_name"])
def test_unknown_tool_repair_does_not_redirect_unfinished_actions_to_done(
    native: bool, restored: bool, request_kind: str,
) -> None:
    session = session_for("FACT_928")
    snapshot = build_turn_snapshot(session, native=native)
    if restored:
        snapshot = restore_snapshot(snapshot_description(snapshot))
    assert "read_file" in snapshot.tool_names
    rejected: str | AssistantTurn
    if native:
        rejected = (AssistantTurn(tool_calls=(ProviderToolCall("unknown-1", "imaginary_tool", {}),))
                    if request_kind == "unknown" else AssistantTurn(finish=TurnFinish.PROTOCOL_ERROR, raw={
                        "protocol_error": "invalid JSON at line 1, column 18",
                        "protocol_tool_name": "imaginary_tool" if request_kind == "malformed_unknown" else ""}))
    else:
        rejected = ('{"tool":"imaginary_tool","args":{}}' if request_kind == "unknown" else
                    '{"tool":"imaginary_tool","args":' if request_kind == "malformed_unknown" else '{"args":')
    plan = normalize_turn(decode_text_turn(rejected) if isinstance(rejected, str) else rejected, snapshot=snapshot)
    assert plan.protocol_error and not plan.calls and plan.control is None
    prompt = _repair_prompt(plan, snapshot=snapshot, native=native, reply=rejected)
    assert "Complete done tool call example" not in prompt and "Native done arguments example" not in prompt
    assert "exactly one property: paragraphs" not in prompt
    assert "read_file" in prompt
    assert ("Available tools:" if native else "Available tool signatures:") in prompt
    if request_kind != "unknown":
        assert "line 1, column" in prompt


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("restored", [False, True])
def test_malformed_explanation_repair_has_one_complete_frozen_example(native: bool, restored: bool) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control"}), required_checks=(declaration("json_explanation"),)), task_kind="chat")
    snapshot = build_turn_snapshot(session, native=native)
    if restored:
        snapshot = restore_snapshot(snapshot_description(snapshot))
    done = next(spec for spec in snapshot.model_specs if spec.name == "done")
    original = parse_json_object(EXPLANATION_DONE_EXAMPLE)
    malformed = '{"tool":"done","args":{"paragraphs":["Incomplete'
    raw: JsonObject = {"protocol_error": "provider returned malformed tool calls", "protocol_tool_name": "done"}
    rejected = AssistantTurn(finish=TurnFinish.PROTOCOL_ERROR, raw=raw) if native else malformed
    plan = normalize_turn(decode_text_turn(rejected) if isinstance(rejected, str) else rejected, snapshot=snapshot)
    assert plan.protocol_error and not plan.calls
    assert plan.protocol_tool_name == "done"
    prompt = _repair_prompt(plan, snapshot=snapshot, native=native, reply=rejected)
    expected = (json.dumps(original["args"], ensure_ascii=False, separators=(",", ":")) if native else
                EXPLANATION_DONE_EXAMPLE)
    label = "Native done arguments" if native else "Complete done tool call"
    for text in (prompt, _completion_followup("Only repair the explanation.", native=native, done_spec=done),
                 kernel_prompt_for_session(session, tool_names=snapshot.tool_names, native=native,
                                           done_spec=done, request_scoped_contract=True)):
        assert text.count(label + " example") == 1
        assert text.count(expected) == 1
        assert "exactly one property: paragraphs" in text
        assert "Do not add summary, report_sections or any other property." in text
        assert "ACTUAL_TOOL_NAME" not in text and '"summary":' not in text and '"report_sections":' not in text
    sample = prompt.split(label + " example (replace sample content with verified task facts): ", 1)[1]
    decoded = parse_json_object(sample[:len(expected)])
    accepted = (AssistantTurn(tool_calls=(ProviderToolCall("repaired", "done", decoded),)) if native else
                decode_text_turn(json.dumps(decoded)))
    repaired = normalize_turn(accepted, snapshot=snapshot)
    assert not repaired.protocol_error and repaired.control is not None
    assert repaired.control.body == "First paragraph.\n\nSecond paragraph."


def test_shared_done_does_not_advertise_a_research_report() -> None:
    from codey.research.tool_contract import TOOL_CONTRACTS, done_spec_for_purpose

    shared = spec_for_tool("done")
    assert shared is not None
    assert "report_sections" not in dict(shared.parameters)
    assert "report_sections" not in shared.description
    properties = decode_json_object(canonical_json_object(TOOL_CONTRACTS["done"].input_schema)["properties"])
    assert "report_sections" not in properties
    research = done_spec_for_purpose(shared, "report")
    assert research.required == ("report_sections",)
    assert set(dict(research.parameters)) == {"report_sections"}


@pytest.mark.parametrize("kind", ["chat", "hybrid"])
@pytest.mark.parametrize("native", [False, True])
def test_explanation_selects_its_contract_without_other_completion_transforms(
    monkeypatch: pytest.MonkeyPatch, kind: str, native: bool,
) -> None:
    def unrelated_contract(*args: object, **kwargs: object) -> ToolSpec:
        raise AssertionError("Explanation must select its own contract directly")

    monkeypatch.setattr("codey.research.tool_contract.done_spec_for_purpose", unrelated_contract)
    monkeypatch.setattr("codey.operations.chat_knowledge.chat_knowledge_spec", unrelated_contract)
    monkeypatch.setattr("codey.research.tool_contract.question_bound_spec", unrelated_contract)
    monkeypatch.setattr("codey.research.answer_contract.answer_spec", unrelated_contract)
    session = TaskSession(TaskPolicy(frozenset({"control"}), required_checks=(declaration("json_explanation"),)), task_kind=kind)
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session, native=native)))
    done = next(spec for spec in snapshot.model_specs if spec.name == "done")
    assert done.required == ("paragraphs",)
    assert set(dict(done.parameters)) == {"paragraphs"}
    assert not normalize_turn(decode_text_turn(done.json_examples[0]), snapshot=snapshot).protocol_error


@pytest.mark.parametrize("kind", ["project", "research"])
@pytest.mark.parametrize("native", [False, True])
def test_strict_research_selects_its_contract_without_explanation_or_chat(
    monkeypatch: pytest.MonkeyPatch, kind: str, native: bool,
) -> None:
    def unrelated_contract(*args: object, **kwargs: object) -> ToolSpec:
        raise AssertionError("Strict Research must keep its own contract")

    monkeypatch.setattr("codey.policies.output_requirement.explanation_spec", unrelated_contract)
    monkeypatch.setattr("codey.operations.chat_knowledge.chat_knowledge_spec", unrelated_contract)
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "knowledge.write"}), strict_research=True), task_kind=kind,
                          task_text="Explain the observed restriction.")
    session.research_questions = ("What is the restriction?",)
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session, native=native)))
    name = "submit_research_answer"
    selected = next(spec for spec in snapshot.model_specs if spec.name == name)
    assert selected.required == ("markdown", "state")
    assert "paragraphs" not in dict(selected.parameters) and "summary" not in dict(selected.parameters)
    assert not normalize_turn(decode_text_turn(selected.json_examples[0]), snapshot=snapshot).protocol_error
    if kind == "research":
        post_submission = next(spec for spec in snapshot.model_specs if spec.name == "done")
        assert post_submission.required == ("summary",) and "knowledge_refs" not in dict(post_submission.parameters)
    else:
        source = next(spec for spec in snapshot.frozen_specs if spec.name == "open_url")
        assert "question_ref" not in source.required


def test_explanation_cannot_override_strict_research_at_snapshot_entry() -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"}), strict_research=True,
                                     required_checks=(declaration("json_explanation"),)), task_kind="research")
    with pytest.raises(ValueError, match="cannot replace the strict Research"):
        build_turn_snapshot(session)


@pytest.mark.parametrize("native", [False, True])
def test_explanation_repair_preserves_the_actual_missing_fact_feedback(native: bool) -> None:
    session = session_for("FACT_928")
    session.last_done_text = "The explanation omits the required fact."
    context = CompletionContext()
    output = prepare_completion_output(session, session.last_done_text, context)
    verdict = evaluate(session, output.text, context=context, observations=output.observations)
    assert verdict.error_source == "completion_explanation" and "answer_text_missing" in verdict.followup
    snapshot = build_turn_snapshot(session, native=native)
    done = next(spec for spec in snapshot.model_specs if spec.name == "done")
    decision = RetryDecision(purpose="retry", error="", prompt=_completion_followup(verdict.followup, native=native, done_spec=done))
    prompt = explanation_repair_prompt(session, context, snapshot, native=native, decision=decision)
    assert verdict.followup in prompt
    assert "answer_text_missing" in prompt and "FACT_928" in prompt
    assert "done.paragraphs" in prompt and "done.summary" not in prompt


@pytest.mark.parametrize("native", [False, True])
def test_restore_rejects_obsolete_explanation_contract_even_with_supported_snapshot_version(native: bool) -> None:
    from codey.research.tool_contract import done_spec_for_purpose

    session = session_for("FACT_928")
    snapshot = build_turn_snapshot(session, native=native)
    base = spec_for_tool("done")
    assert base is not None
    report = done_spec_for_purpose(base, "report")
    obsolete = freeze_spec_parameters(replace(base, parameters=(*base.parameters, *report.parameters), required=()))
    snapshot = refresh_snapshot_contract(replace(snapshot, ignore_runtime_claims=True,
        frozen_specs=tuple(obsolete if spec.name == "done" else spec for spec in snapshot.frozen_specs)))
    payload = snapshot_description(snapshot)
    payload["implementation_id"] = "previous-build"
    assert restore_snapshot(snapshot_description(build_turn_snapshot(session, native=native))).policy == session.policy
    with pytest.raises(ValueError, match="unsupported captured explanation contract"):
        restore_snapshot(payload)


def test_exact_omission_never_removes_undeclared_values_or_identifier_substrings() -> None:
    binding = require_tool_success("browser_eval", answer_pointer="/token", result_pointer="/token", omit_from_explanation=True)
    rule = parse_tool_success_check(binding)
    assert rule is not None
    from codey.policies.requirement import RequirementEvaluation

    observations = {rule.check_id: ToolObservation(RequirementEvaluation(), "TOKEN_123")}
    draft = "TOKEN_123 larger_TOKEN_123 TOKEN_123_extra UNDECLARED_88"
    assert explanation_text(draft, (binding,), observations) == " larger_TOKEN_123 TOKEN_123_extra UNDECLARED_88"
    assert explanation_text(draft, (), observations) == draft


def test_declared_content_edit_does_not_offer_a_conflicting_field_edit_template() -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})), task_kind="hybrid",
                          task_text="完整 read_file 后，使用 content 写入完整 JSON，以外层 Result.result_ref 为 base_result_ref。")
    snapshot = build_turn_snapshot(session)
    edit = next(spec for spec in snapshot.frozen_specs if spec.name == "edit")
    assert set(edit.required) == {"path", "content", "base_result_ref"}
    assert "declared_fields" not in dict(edit.parameters)
    declarations = render_declarations(snapshot_catalog(snapshot))
    assert "changes: inputs.changes" not in declarations and "Pass the original complete read result" not in declarations
    prepared = prepare_kernel_turn(session, user_task=session.task_text, context_text="", task_guidance="", native=False)
    assert "changes: inputs.changes" not in prepared.prompt


def test_bound_field_entry_and_catalog_offer_the_same_receipt_copy_template() -> None:
    binding = require_browser_file_field(url="https://example.com/record", prefix="Amount: ", suffix=" units.",
        path="record.json", pointer="/amount", value_type="number")
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"}), required_checks=(binding,)),
                          task_kind="hybrid", task_text="Read record.json and update its declared amount.")
    snapshot = build_turn_snapshot(session)
    edit = next(spec for spec in snapshot.frozen_specs if spec.name == "edit")
    fields = dict(edit.parameters)
    assert {"path", "base_result_ref", "declared_fields"} <= fields.keys()
    assert canonical_json_object(fields["declared_fields"])["enum"] == [True]
    declarations = render_declarations(snapshot_catalog(snapshot))
    prepared = prepare_kernel_turn(session, user_task=session.task_text, context_text="", task_guidance="", native=False)
    assert "declared_fields:true" in declarations and "declared_fields:true" in prepared.prompt
    assert "changes: inputs.changes" not in declarations and "changes: inputs.changes" not in prepared.prompt


def test_wrong_receipt_view_returns_an_exact_read_call_without_aliasing_values() -> None:
    session = TaskSession(TaskPolicy(frozenset({"control"})))
    session._memory_results["read-1"] = ToolResult(ToolCall("browser_read", {}, "read-1"), "complete body", ok=True)
    reader = ReceiptReader(session, None, "session", "run")
    wrong = read_tool_result(reader, ToolCall("read_tool_result", {"result_ref": "read-1", "view": "value"}))
    assert not wrong.ok and "available views: output" in wrong.model_text
    next_read = decode_json_object(wrong.canonical["next_read"])
    assert next_read["tool"] == "read_tool_result"
    args = decode_json_object(next_read["args"])
    assert args["view"] == "output"
    right = read_tool_result(reader, ToolCall("read_tool_result", args))
    assert right.ok and parse_json_object(right.model_text)["text"] == "complete body"
    assert "value" not in session._memory_results["read-1"].canonical


@pytest.mark.parametrize("url", ["https://example.com/ledger", "https://example.org/inventory?lot=9"])
@pytest.mark.parametrize("token", ["META_411", "AUDIT_982"])
@pytest.mark.parametrize(("amount", "code"), [("173", "QUARTZ_851"), ("-0.125000031", "BATCH_009")])
def test_synthetic_fact_variants_keep_business_facts_and_only_omit_bound_metadata(url: str, token: str, amount: str, code: str) -> None:
    bindings = (
        require_tool_success("browser_eval", answer_pointer="/token", result_pointer="/value", omit_from_explanation=True),
        require_tool_success("browser_tab_list", answer_pointer="/page_url", result_pointer="/page_url", omit_from_explanation=True),
        require_browser_read_field(url=url, prefix="Weight: ", suffix=" kg.", answer_pointer="/explanation", answer_contains=True),
        require_browser_read_field(url=url, prefix="Code: ", suffix=".", answer_pointer="/explanation", answer_contains=True),
    )
    schema: JsonObject = {"type": "object", "properties": {
        "explanation": {"type": "string", "pattern": r"\A[^\n]+\n\n[^\n]+\Z"},
        "token": {"type": "string"}, "page_url": {"type": "string"}},
        "required": ["explanation", "token", "page_url"], "additionalProperties": False}
    session = TaskSession(TaskPolicy(frozenset({"control"}), required_checks=(
        declaration("json_explanation", json.dumps(schema)), *bindings)), task_kind="chat")
    document: JsonObject = {"url": url, "document_id": "current"}
    record(session, "browser_click", {}, document)
    read = record(session, "browser_read", {}, document, body=f"Weight: {amount} kg. Code: {code}.")
    parent = ToolResult(ToolCall("codemode", {}, "parent"), "committed", ok=True, canonical={"child_effect_ids": [read]})
    session._memory_results["parent"] = parent
    project_execution_record(session, "parent", parent.call, parent, ok=True)
    ref = record(session, "browser_eval", {}, document)
    session._memory_results[ref] = replace(session._memory_results[ref], canonical={"value": {"value": token, "truncated": False}})
    record(session, "browser_tab_list", {}, document)
    draft = f"Weight {amount} kg, URL {url}.\n\nCode {code}, token {token}."
    context = CompletionContext(browser_document=document)
    output = prepare_completion_output(session, draft, context)
    assert evaluate(session, output.text, context=context, observations=output.observations).complete
    delivered = parse_json_object(output.text)
    assert delivered["token"] == token and delivered["page_url"] == url
    assert delivered["explanation"] == draft.replace(url, "").replace(token, "")
    bad = prepare_completion_output(session, draft.replace(code, "UNSUPPORTED_33"), context)
    assert not evaluate(session, bad.text, context=context, observations=bad.observations).complete
    assert sum(row.call.name == "browser_click" for row in session._memory_results.values()) == 1
    assert sum(row.call.name == "browser_read" for row in session._memory_results.values()) == 1


@pytest.mark.parametrize(("code", "wrong"), [
    ("return unwrap(await tools.browser_read({})).text;", True),
    ("const body=unwrap(await tools.browser_read({}));return body.body;", True),
    ("const body=unwrap(await tools.browser_read({}));return body.trim();", False),
    ("const body=inputs.body;return body.text;", False),
    ("return unwrap(await tools.browser_read({}));", False),
])
def test_schema_diagnostic_is_limited_to_known_tool_value_shapes(code: str, wrong: bool) -> None:
    catalog = build_catalog_snapshot([ToolCatalogEntry("browser_read", "Read", output_schema={"type": "string"})])
    assert bool(shape_diagnostic(code, catalog)) is wrong


@pytest.mark.parametrize(("ok", "read_only", "extra"), [(True, True, False), (False, True, False), (True, False, False), (True, True, True)])
def test_shape_repair_requires_every_child_settled_and_read_only(ok: bool, read_only: bool, extra: bool) -> None:
    catalog = build_catalog_snapshot([ToolCatalogEntry("read_file", "Read", read_only=read_only)])
    result = ToolResult(ToolCall("read_file", {}, "run:parent:child:0"), "body", ok=ok)
    results = {"r0": result}
    if extra:
        results["unaccounted"] = replace(result, call=ToolCall("read_file", {}, "run:parent:child:1"))
    assert readonly_settled("run:parent", ["r0"], results, catalog) is (ok and read_only and not extra)


def run_code(session: TaskSession, code: str, turn: int, executors: dict[str, ToolExecutor], token: str = "") -> ToolResult:
    snapshot = build_turn_snapshot(session)
    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="shape-run", effect_scope="task",
                                         turn=turn, executors=executors, session_id="shape-session")
    args: JsonObject = {"code": code}
    if token:
        args["repair_of"] = token
    return execute_turn(session, [ToolCall("codemode", args, call_id=f"call-{turn}")], run_id="shape-run", effect_scope="task",
                         turn=turn, snapshot=snapshot, executors={"codemode": parent})[0]


def test_schema_shape_repair_reuses_original_production_receipt_once(tmp_path: Path) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "web.read"})), project=str(tmp_path))
    calls: list[ToolCall] = []

    def source(call: ToolCall) -> ToolResult:
        calls.append(call)
        body = "original source body"
        url = str(call.args["url"])
        opened = OpenedSource(url, url, "Record", "2026-11-15T00:00:00Z", hashlib.sha256(body.encode()).hexdigest()[:16])
        return ToolResult(call, body, ok=True, canonical={"research_observation": source_snapshot(opened, body, {}),
            "read_passages": [{"start": 0, "end": len(body)}]})

    bad = run_code(session, "const body=unwrap(await tools.open_url({url:'https://example.com/record'}));return body.text.trim();",
                   1, {"open_url": source})
    assert not bad.ok and bad.audit.get("repair_category") == "shape_mismatch", bad.model_text
    metadata = decode_json_object(bad.canonical["repair"])
    assert metadata["failure_code"] == "shape_mismatch"
    token = str(metadata["token"])
    good_code = "return unwrap(await tools.open_url({url:'https://example.com/record'}));"
    repaired = run_code(session, good_code, 2, {"open_url": source}, token)
    assert repaired.ok and repaired.audit.get("recovery_disposition") == "receipt_replay", repaired
    assert repaired.canonical["value"] == "original source body" and len(calls) == 1
    reused = run_code(session, good_code, 3, {"open_url": source}, token)
    assert not reused.ok and reused.audit.get("error_code") == "repair_rejected"
    assert len(calls) == 1


def last_snapshot(directory: Path) -> JsonObject:
    snapshots = [decode_json_object(body["payload"])["snapshot"] for _, body in artifacts(directory / "state")
                 if body.get("kind") == "provider_request"]
    return decode_json_object(snapshots[-1])


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("native", [False, True])
def test_repair_stays_restricted_after_receipt_read_protocol_retry_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    directory = tmp_path / "repair"
    if native:
        monkeypatch.setattr("tools.browser_production_live_gate.ExperimentProvider", NativeExperimentProvider)

    def assert_repair(expected_error: str = "") -> None:
        snapshot = last_snapshot(directory)
        names = snapshot["tool_names"]
        assert isinstance(names, list) and "done" in names and set(names) <= {"done", "read_tool_result"}
        assert snapshot["nested_tool_names"] == []
        restored = restore_snapshot(snapshot)
        done = next(spec for spec in restored.model_specs if spec.name == "done")
        assert done.required == ("paragraphs",)
        assert set(dict(done.parameters)) <= {"paragraphs", CANDIDATE_FIELD}
        valid = normalize_turn(decode_text_turn(done.json_examples[0]), snapshot=restored)
        assert not valid.protocol_error
        assert schema_for_spec(done)["additionalProperties"] is False
        messages = provider_requests(directory)[-1]["messages"]
        assert isinstance(messages, list)
        content = [decode_json_object(message).get("content") for message in messages]
        prompt = "\n".join(item for item in content if isinstance(item, str))
        if expected_error:
            assert expected_error in prompt

    def replies() -> Iterator[JsonObject]:
        yield code_action(LOAD + WAIT + "return null;")
        yield {"tool": "browser_read", "args": {}}
        for _ in range(2):
            names = last_snapshot(directory)["tool_names"]
            assert isinstance(names, list)
            name = next(name for name in ("browser_eval", "browser_tab_list") if name in names)
            yield {"tool": name, "args": {"code": "window.__contractToken"} if name == "browser_eval" else {}}
        yield {"tool": "done", "args": {"paragraphs": ["净重为 173 克，遗漏校验码。"]}}
        assert_repair("answer_text_missing")
        read = next(row for row in tool_receipts(directory / "state") if row.result.call.name == "browser_read")
        yield {"tool": "read_tool_result", "args": {"result_ref": read.identity, "view": "output"}}
        assert_repair()
        yield {"tool": "done", "args": {"paragraphs": ["净重 173 克。"], "report_sections": {}}}
        assert_repair("unexpected arg 'report_sections'")
        yield {"tool": "done", "args": {"paragraphs": ["已读到净重 173 克。", "校验码 QUARTZ_851，SCHEMA_TOKEN_74。"]}}

    install_protocol_http(monkeypatch, replies(), native=native)
    row = run_case(directory, "chat", Target("http://scripted.test/v1", "fixture", 123456789, 10),
                   explanation_output=True, restore_snapshots=True, max_turns=10)
    assert row["passed"] is True, row
    names = [receipt.result.call.name for receipt in tool_receipts(directory / "state")]
    assert names.count("browser_click") == names.count("browser_read") == 1
    assert names.count("read_tool_result") == 1


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("failure", ["completion", "protocol"])
def test_explanation_repair_exhausts_a_finite_budget_without_repeating_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    directory = tmp_path / "exhausted"

    def replies() -> Iterator[JsonObject]:
        yield code_action(LOAD + WAIT + "return null;")
        yield {"tool": "browser_read", "args": {}}
        for _ in range(2):
            names = last_snapshot(directory)["tool_names"]
            assert isinstance(names, list)
            name = next(name for name in ("browser_eval", "browser_tab_list") if name in names)
            yield {"tool": name, "args": {"code": "window.__contractToken"} if name == "browser_eval" else {}}
        for index in range(6):
            args: JsonObject = {"paragraphs": [f"缺少业务事实的单段说明 {index if failure == 'completion' else 0}。"]}
            if failure == "protocol" and index:
                args["report_sections"] = dict[str, JsonValue]()
            yield {"tool": "done", "args": args}

    install_http(monkeypatch, replies())
    row = run_case(directory, "chat", Target("http://scripted.test/v1", "fixture", 123456789, 10),
                   explanation_output=True, restore_snapshots=True, max_turns=14)
    assert row["stop_reason"] == "answer_repair_exhausted", row
    names = [receipt.result.call.name for receipt in tool_receipts(directory / "state")]
    assert names.count("browser_click") == names.count("browser_read") == 1


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_ordinary_hybrid_rejects_same_program_edit_then_copies_from_receipt_without_browser_repeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "hybrid"
    def replies() -> Iterator[JsonObject]:
        yield code_action(LOAD + WAIT + "unwrap(await tools.browser_read({}));"
            "const read=await tools.read_file({path:'result.json'});"
            "return unwrap(await tools.edit({base_result_ref:read.result_ref,changes:{amount:'173',code:'QUARTZ_851'}}));")
        binding = restore_snapshot(last_snapshot(directory)).input_binding
        assert binding is not None and binding.kind == "declared_fields"
        yield {"tool": binding.model_spec.name, "args": {"path": "result.json"}}
        yield {"tool": "done", "args": {"summary": "已写入净重 173 克和校验码 QUARTZ_851。"}}

    install_http(monkeypatch, replies())
    row = run_case(directory, "hybrid", Target("http://scripted.test/v1", "fixture", 123456789, 10),
                   declare_required_tools=False, restore_snapshots=True, max_turns=7)
    assert row["passed"] is True, row
    receipts = tool_receipts(directory / "state")
    rejected = [receipt.result for receipt in receipts if receipt.result.call.name == "codemode" and not receipt.result.ok]
    assert len(rejected) == 1
    assert "The declared file source was acquired in this program" in rejected[0].model_text
    edits = [receipt.result for receipt in receipts if receipt.result.call.name == "edit"]
    # The source-acquisition boundary rejects the original program before an
    # edit executes. The next frozen input copies the source's numeric type.
    assert len(edits) == 1 and edits[0].ok
    assert parse_json_object((directory / "project-result.json").read_text(encoding="utf-8")) == {
        "amount": 173, "code": "QUARTZ_851",
    }
    assert sum(receipt.result.call.name == "browser_read" for receipt in receipts) == 1
