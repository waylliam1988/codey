"""Task requirements stay bound across browser actions and answer repair."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from codey.codemode.json_edit import JsonNumber, replace_json_fields
from codey.operations.browser_copy import select_source
from codey.operations.kernel_facts import project_execution_record
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import build_task_policy
from codey.policies.tool_requirement import ToolSuccessRequirement, parse_tool_success_check
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_prerequisite_ab import LOAD, WAIT, code_action
from tools.browser_production_live_gate import run_case
from tools.completion_artifact_audit import artifacts, tool_receipts
from tools.completion_explanation_ab import Target


def test_ordinary_entry_materializes_explicit_wait_without_granting_access() -> None:
    policy = build_task_policy(SimpleNamespace(
        task="点击查询按钮，调用 browser_wait 等待出现结算完成，最后 browser_read 当前页。",
        project=None,
    ), task_kind="hybrid")
    rules = [rule for check in policy.required_checks if (rule := parse_tool_success_check(check)) is not None]
    wait = next(rule for rule in rules if rule.tool == "browser_wait")
    assert '"value":"结算完成"' in wait.arguments_json
    assert not policy.allows("browser.interact")


@pytest.mark.parametrize("task", [
    "不要调用 browser_wait。", "示例：```browser_wait 等待出现结算完成```", "只读这个文件，不用操作浏览器。",
    "示例：\"调用 browser_wait 等待出现结算完成\"。",
    "打开 https://example.com/a 和 https://example.com/b，调用 browser_wait 等待出现结算完成。",
])
def test_wait_mentions_do_not_create_requirements(task: str) -> None:
    policy = build_task_policy(SimpleNamespace(task=task, project=None), task_kind="hybrid")
    assert not policy.required_checks


def test_forbidden_metadata_feedback_targets_explanation_only() -> None:
    rule = ToolSuccessRequirement("browser_eval", answer_pointer="/token", result_pointer="/token",
                                  omit_from_explanation=True)
    failure = rule._answer_failure("answer_text_forbidden", "TOKEN_NOT_PROSE", ["receipt-1"])
    assert "Repair only /explanation" in failure.feedback
    assert "Repair only the answer at pointer '/token'" not in failure.feedback
    assert "Expected selected value" not in failure.feedback


def test_decimal_source_retains_more_precision_than_javascript_numbers() -> None:
    data: JsonObject = {"tables": [], "records": [{"id": "json-1", "text":
        '{"balance":123456789012345.67890123456789}'}]}
    value = select_source(data, {"id": "json-1", "pointer": "/balance"}, "number")
    assert str(value) == "123456789012345.67890123456789"


@pytest.mark.parametrize("text", ["-0.1250", "0", "1.50e-12", "12345678901234567890.12345678901234567890"])
def test_exact_numbers_write_without_changing_unrelated_json(text: str) -> None:
    original = '{ "balance": 0, "untouched": 9007199254740993.123456789 }\n'
    assert replace_json_fields(original, {"balance": JsonNumber(text)}) == original.replace('"balance": 0', '"balance": ' + text)


@pytest.mark.parametrize("text", ["NaN", "Infinity", "01", "1,234.50", "12元", '1,"other":true', "1.2.3"])
def test_ambiguous_or_invalid_number_text_is_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        JsonNumber(text)


def test_source_json_preserves_strings_and_rejects_boolean_as_number() -> None:
    data: JsonObject = {"tables": [], "records": [{"id": "json-1", "text": '{"id":"00017","flag":true}'}]}
    assert select_source(data, {"id": "json-1", "pointer": "/id"}, "string") == "00017"
    with pytest.raises(ValueError):
        select_source(data, {"id": "json-1", "pointer": "/flag"}, "number")


@pytest.mark.parametrize("case", ["valid", "before_click", "other_document", "after_new_click", "failed_wait"])
def test_wait_and_read_require_the_correct_document_and_order(case: str) -> None:
    policy = build_task_policy(SimpleNamespace(task="点击后调用 browser_wait 等待出现已结算，最后 browser_read。"), task_kind="hybrid")
    session = TaskSession(policy, task_kind="hybrid")
    document: JsonObject = {"url": "https://example.com/settlement", "document_id": "current"}

    def record(name: str, *, other: bool = False, ok: bool = True) -> None:
        ref = str(len(session.executed))
        args: JsonObject = {"condition": "text", "value": "已结算"} if name == "browser_wait" else {}
        result = ToolResult(ToolCall(name, args, call_id=ref), "receipt", ok=ok,
            canonical={"value": {"matched": ok, "page_url": document["url"]}},
            audit={"browser_document": {**document, "document_id": "old"} if other else document})
        project_execution_record(session, ref, result.call, result, ok=ok)
        session._memory_results[ref] = result

    if case == "before_click":
        record("browser_wait")
    record("browser_click")
    if case != "before_click":
        record("browser_wait", other=case == "other_document", ok=case != "failed_wait")
    record("browser_read")
    if case == "after_new_click":
        record("browser_click")
    wait = next(rule for check in policy.required_checks
                if (rule := parse_tool_success_check(check)) is not None and rule.tool == "browser_wait")
    assert wait.observe(session.executed, results=session._memory_results, browser_document=document).evaluation.passed is (case == "valid")


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_explanation_repair_keeps_metadata_and_does_not_reopen_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "structured"

    def replies() -> Iterator[JsonObject]:
        yield code_action(LOAD + WAIT + "return null;")
        yield {"tool": "browser_read", "args": {}}
        for _ in range(2):
            snapshots = [decode_json_object(body["payload"])["snapshot"] for _, body in artifacts(directory / "state")
                         if body.get("kind") == "provider_request"]
            names = decode_json_object(snapshots[-1])["tool_names"]
            assert isinstance(names, list)
            name = next(name for name in ("browser_eval", "browser_tab_list") if name in names)
            yield {"tool": name, "args": {"code": "window.__contractToken"} if name == "browser_eval" else {}}
        yield {"tool": "done", "args": {"summary": "已读取净重 173 克。\n\n校验码 QUARTZ_851，SCHEMA_TOKEN_74。"}}
        snapshots = [decode_json_object(body["payload"])["snapshot"] for _, body in artifacts(directory / "state")
                     if body.get("kind") == "provider_request"]
        last = decode_json_object(snapshots[-1])
        names = last["tool_names"]
        assert isinstance(names, list) and "done" in names and set(names) <= {"done", "read_tool_result"}
        assert last["nested_tool_names"] == []
        yield {"tool": "done", "args": {"summary": "已读取当前记录，净重为 173 克。\n\n记录校验码为 QUARTZ_851。"}}

    install_http(monkeypatch, replies())
    result = run_case(directory, "chat", Target("http://scripted.test/v1", "fixture", 123456789, 10),
                      explanation_output=True, restore_snapshots=True, max_turns=6)
    assert result["passed"] is True, result
    calls = [row.result.call.name for row in tool_receipts(directory / "state") if row.result.ok]
    assert {name: calls.count(name) for name in ("browser_click", "browser_read", "browser_eval")} == {
        "browser_click": 1, "browser_read": 1, "browser_eval": 1}
