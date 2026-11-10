"""Candidate wrapping never repairs tool calls or weakens exact-field checks."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.policies.output_requirement import JSON_ANSWER_GUIDANCE
from codey.policies.tool_requirement import decode_answer, parse_tool_success_check
from codey.types.json_value import JsonObject, decode_json_object
from tools.completion_explanation_ab import (
    BINDINGS,
    PROSE_RULE,
    URL,
    adapt_reply,
    audit_run,
    explanation_checks,
    final_checks,
    production_source_digest,
    prose_guidance,
    provider_metrics,
)


def test_candidate_preserves_prose_and_only_wraps_summary() -> None:
    explanation = '点击 "Load record"。\n路径示例 C:\\record\\draft；净重 173 克，QUARTZ_851。'
    action: JsonObject = {"tool": "done", "args": {"summary": explanation, "knowledge_refs": []}}
    original = json.dumps(action, ensure_ascii=False)
    adapted, raw = adapt_reply(original, "B")
    decoded = decode_json_object(decode_answer(adapted))
    args = decode_json_object(decoded["args"])
    assert raw == explanation
    assert decode_answer(str(args["summary"])) == {"explanation": explanation}
    assert args["knowledge_refs"] == []
    assert "page_url" not in str(args["summary"]) and "token" not in str(args["summary"])
    assert adapt_reply(original, "A") == (original, explanation)


@pytest.mark.parametrize("reply", [
    '{"tool":"done","args":{"summary":"bad\\escape"}}',
    '{"tool":"done","args":{"summary":3}}',
    '{"tool":"done","args":{"summary":"a","summary":"b"}}',
    'ordinary prose without the required outer tool envelope',
    '{"tool":"codemode","args":{"code":"// comment\\nawait tools.browser_wait({});"}}',
])
def test_candidate_never_repairs_outer_json_or_other_actions(reply: str) -> None:
    assert adapt_reply(reply, "B") == (reply, None)


def test_json_looking_prose_is_not_implicitly_unpacked() -> None:
    text = '{"explanation":"still raw text","page_url":"model guess"}'
    adapted, _ = adapt_reply(json.dumps({"tool": "done", "args": {"summary": text}}), "B")
    args = decode_json_object(decode_json_object(decode_answer(adapted))["args"])
    assert decode_answer(str(args["summary"])) == {"explanation": text}


def test_guidance_preserves_execution_requirements_and_removes_inner_json_demand() -> None:
    for binding in BINDINGS:
        requirement = parse_tool_success_check(binding)
        assert requirement is not None
        output = prose_guidance(requirement.guidance)
        assert output.startswith("Required execution:")
        assert "parent must both commit successfully" in output
        assert PROSE_RULE in output
        assert JSON_ANSWER_GUIDANCE not in output
        assert "supply only unbound JSON fields" not in output
    assert prose_guidance("observations and browser action contract") == "observations and browser action contract"


def test_exact_fields_cannot_be_proven_by_correct_strings_elsewhere() -> None:
    value: JsonObject = {"explanation": f"173 克，QUARTZ_851；{URL}；SCHEMA_TOKEN_74", "page_url":
                         "https://example.com/production_browser_record", "token": "SCHEMA_TOKEN_74"}
    checks = final_checks(json.dumps(value))
    assert checks["schema_valid"] is True and checks["exact_fields"] is False
    value["page_url"] = URL
    assert final_checks(json.dumps(value))["exact_fields"] is True
    value["token"] = "wrong"
    assert final_checks(json.dumps(value))["exact_fields"] is False


@pytest.mark.parametrize("explanation", [None, "", 173])
def test_missing_or_wrong_explanation_is_not_a_pass(explanation: object) -> None:
    checks = final_checks(json.dumps({"explanation": explanation, "page_url": URL, "token": "SCHEMA_TOKEN_74"}))
    assert checks["schema_valid"] is False


def test_usage_is_unknown_when_a_response_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "provider.jsonl"
    request: JsonObject = {"type": "wire_attempt", "phase": "request", "exchange": 1}
    response: JsonObject = {"type": "response", "exchange": 1, "seconds": 1.5,
                            "payload": {"usage": {"prompt_tokens": 10, "completion_tokens": 3}}}
    path.write_text("\n".join(json.dumps(row) for row in (request, response)), encoding="utf-8")
    assert provider_metrics(path)["completion_tokens"] == 3
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n" + json.dumps({**request, "exchange": 2}))
    metrics = provider_metrics(path)
    assert metrics["model_calls"] == 2 and metrics["usage_complete"] is False
    assert metrics["completion_tokens"] is None


def test_audit_distinguishes_real_newlines_from_literal_escapes_and_exact_list_count() -> None:
    assert explanation_checks("观察段。\n\n结果段。", 1)["requested_shape_valid"] is True
    assert explanation_checks("观察段。\\n\\n结果段。", 1)["requested_shape_valid"] is False
    assert explanation_checks("- 操作\n- 结果", 3)["requested_shape_valid"] is True
    assert explanation_checks("- 操作\n- 结果\n- 额外一项", 3)["requested_shape_valid"] is False


def test_audit_reports_wrong_urls_even_when_a_correct_url_is_also_present() -> None:
    explanation = f'{URL}\nhttps://example.com/explanation_browser_record SCHEMA_TOKEN_75 点击 "Load record"'
    checks = explanation_checks(explanation, 2)
    assert checks["requested_shape_valid"] is True
    assert checks["bound_values_repeated"] is True
    assert checks["unexpected_url_mentions"] == ["https://example.com/explanation_browser_record"]
    assert checks["unexpected_token_mentions"] == ["SCHEMA_TOKEN_75"]


def test_read_only_audit_keeps_original_failure_and_primary_summary(tmp_path: Path) -> None:
    directory = tmp_path / "01" / "A"
    directory.mkdir(parents=True)
    source = production_source_digest()
    summary: JsonObject = {"source_before": source, "source_after": source, "results": [{
        "pair": 1, "arm": "A", "passed": False, "stop_reason": "no_progress",
        "required_executions_present": True}]}
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(summary), encoding="utf-8")
    original = path.read_bytes()
    (tmp_path / "manifest.json").write_text(json.dumps({"interface": "explanation-wrapper-ab-v1", "seed": 7}),
                                            encoding="utf-8")
    (directory / "proposals.jsonl").write_text(json.dumps({"raw_summary": '{"explanation":"click "button""}'}),
                                               encoding="utf-8")
    (directory / "provider.jsonl").write_text(json.dumps({"type": "response", "service": {
        "last_seed": 7, "total_gens": 1}}), encoding="utf-8")
    audited = audit_run(tmp_path)
    assert path.read_bytes() == original
    assert decode_json_object(audited["A"])["first_inner_json_errors"] == 1
    cases = audited["cases"]
    assert isinstance(cases, list)
    assert decode_json_object(cases[0])["primary_passed"] is False
    assert (tmp_path / "audit.json").exists()
