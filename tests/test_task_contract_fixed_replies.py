"""Saved failing replies and controlled alternatives through the production browser gate.

These check diagnostics and characterize remaining task failures; passing pytest
does not mean the task passed. Saved replies retain their original JSON escapes.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, TypedDict

import pytest

from codey.codemode.replay import DurableCodeModeReplay
from codey.codemode.runtime import NodeWorkerRuntime, ToolInvoker
from codey.codemode.types import CodeModeLimits, CodeModeResult, ToolCatalogEntry
from codey.types.json_value import decode_json_object, decode_json_value, parse_json_object
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_full_surface_live_gate as matrix
from tools import browser_prerequisite_ab as browser
from tools.browser_production_live_gate import URL, run_case
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target

FIXTURE = Path(__file__).parent / "fixtures" / "task_contract_failure_replies_20261113.json"
TARGET = Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10)
pytestmark = pytest.mark.usefixtures(
    "public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")


class RequestObserver(Protocol):
    def __call__(self, *, attempt: int, data: bytes, phase: str, response_bytes: int, seconds: float) -> None: ...


class RuntimeExecute(Protocol):
    def __call__(self, runtime: NodeWorkerRuntime, code: str, /, *, catalog: Sequence[ToolCatalogEntry],
                 invoke: ToolInvoker, inputs: Mapping[str, object] | None = None, state_store: object = None,
                 replay_context: Mapping[str, object] | None = None,
                 limits: CodeModeLimits | None = None) -> CodeModeResult: ...


class ControlFlowTrace(TypedDict):
    seen: list[str]
    committed: list[str]
    seen_without_intent: list[str]


def saved_replies(case: str) -> list[str]:
    replies = decode_json_object(parse_json_object(FIXTURE.read_text(encoding="utf-8"))[case])["replies"]
    assert isinstance(replies, list)
    result: list[str] = []
    for reply in replies:
        assert isinstance(reply, str)
        result.append(reply)
    return result


def install_original_http(monkeypatch: pytest.MonkeyPatch, replies: list[str]) -> None:
    remaining = iter(replies)

    def scripted(url: str, payload: dict[str, object], headers: dict[str, str], *,
                 observe: RequestObserver, **kwargs: object) -> dict[str, object]:
        reply = next(remaining)
        observe(attempt=1, data=json.dumps(payload).encode(), phase="request", response_bytes=0, seconds=0.0)
        return {"choices": [{"message": {"role": "assistant", "content": reply, "tool_calls": []},
                              "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    monkeypatch.setattr("codey.providers.api_generation.generate", scripted)

    def service_state(_url: str) -> dict[str, object]:
        return {}

    monkeypatch.setattr("tools.completion_explanation_ab.service_state", service_state)


@pytest.mark.parametrize("case", ["basic", "structured", "stale_ref", "stale_edit"])
def test_original_failure_replies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    replies = saved_replies(case)
    install_original_http(monkeypatch, replies)
    directory = tmp_path / case
    result = matrix._case(tmp_path, case, TARGET)
    assert result["passed"] is False, result
    receipts = [r.result for r in tool_receipts(directory / "state")]
    parents = [r for r in receipts if r.call.name == "codemode"]
    assert all("repair_of" not in r.call.args for r in parents)
    if case == "basic":
        assert len(parents) == 4
        assert all("browser_fill missing required arg 'ref'" in r.model_text for r in parents[1:])
        assert all("recovery did not consume" not in r.model_text for r in parents)
        wrong = URL.replace("production-browser-record", "production_browser_record")
        navigations = decode_json_value(result["navigations"])
        assert isinstance(navigations, list) and navigations.count(wrong) == 3
        snapshots = [r for r in receipts if r.call.name == "browser_snapshot"]
        assert len(snapshots) == 3 and all(r.ok for r in snapshots)
        assert all(decode_json_object(r.canonical["value"])["elements"] == [] for r in snapshots)
        requests = [parse_json_object(line) for line in (directory / "provider.jsonl").read_text(
            encoding="utf-8").splitlines()]
        assert any(row.get("phase") == "request" and "browser_fill missing required arg 'ref'" in json.dumps(
            row.get("payload")) for row in requests)
    elif case == "structured":
        assert result["stop_reason"] == "done"
        output = parse_json_object(str(result["answer"]))
        assert output["page_url"] == URL and output["token"] == "SCHEMA_TOKEN_74"
        assert result["facts_match"] is False
        explanation = output["explanation"]
        assert isinstance(explanation, str) and "173" not in explanation and "QUARTZ_851" not in explanation
        assert any(r.call.name == "browser_read" and r.ok and "QUARTZ_851" in r.model_text for r in receipts)
    elif case == "stale_ref":
        assert len(parents) == 4
        assert result["failed_stale_refs"] == 0
        assert not any(r.call.name == "browser_click" for r in receipts)
        assert all(r.audit.get("error_code") == "syntax_error" for r in parents)
    else:
        successful = decode_json_value(result["successful_tools"])
        assert isinstance(successful, list) and "browser_read" in successful
        assert result["version_check_reached"] is False
        assert result["true_stale_conflicts"] == 0
        assert result["project_result"] == {"amount": 0, "code": "unknown"}
        assert any("read" in r.model_text and not r.ok for r in parents)


@pytest.mark.parametrize("followup", ["snapshot_only", "unchecked_fill", "checked_fill"])
def test_wrong_url_feedback_without_a_prior_compile_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, followup: str,
) -> None:
    runtime_results: list[CodeModeResult] = []
    control_flow: list[ControlFlowTrace] = []
    execute: RuntimeExecute = NodeWorkerRuntime.execute
    assert_valid = DurableCodeModeReplay.assert_valid

    def observed_execute(self: NodeWorkerRuntime, code: str, *, catalog: Sequence[ToolCatalogEntry],
                         invoke: ToolInvoker, inputs: Mapping[str, object] | None = None,
                         state_store: object = None, replay_context: Mapping[str, object] | None = None,
                         limits: CodeModeLimits | None = None) -> CodeModeResult:
        result = execute(self, code, catalog=catalog, invoke=invoke, inputs=inputs, state_store=state_store,
                         replay_context=replay_context, limits=limits)
        runtime_results.append(result)
        return result

    def observed_assert_valid(self: DurableCodeModeReplay) -> None:
        committed = {effect.effect_id for effect in self.durable.effects(self.task_id)
                     if effect.intent.get("parent_effect_id") == self.parent_effect_id}
        control_flow.append({"seen": sorted(self._children_seen), "committed": sorted(committed),
                             "seen_without_intent": sorted(self._children_seen - committed)})
        assert_valid(self)

    monkeypatch.setattr(NodeWorkerRuntime, "execute", observed_execute)
    monkeypatch.setattr(DurableCodeModeReplay, "assert_valid", observed_assert_valid)
    ending = {"snapshot_only": "return s;",
              "unchecked_fill": "await tools.browser_fill({ref,value:'福州'});return s;",
              "checked_fill": "return unwrap(await tools.browser_fill({ref,value:'福州'}));"}[followup]
    code = ("const nav=await tools.browser_navigate({url:inputs.url});"
            "const s=unwrap(await tools.browser_snapshot({}));"
            "const ref=s.elements.find(e=>e.name==='Locality')?.ref || '';" + ending)
    install_http(monkeypatch, [{"tool": "codemode", "args": {"code": code, "inputs": {
        "url": URL.replace("production-browser-record", "production_browser_record")}}}])
    directory = tmp_path / "run"
    run_case(directory, "chat", TARGET, max_turns=1)
    receipts = [r.result for r in tool_receipts(directory / "state")]
    parent, = [r for r in receipts if r.call.name == "codemode"]
    trace: dict[str, object] = {
        "runtime": [{"ok": r.ok, "error_code": r.error_code, "error": r.error,
                     "child_calls": r.child_calls} for r in runtime_results],
        "control_flow": control_flow, "parent_feedback": parent.model_text,
    }
    (directory / "feedback-trace.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    assert "repair_of" not in parent.call.args
    assert any(r.call.name == "browser_snapshot" and r.ok for r in receipts)
    if followup == "snapshot_only":
        assert parent.ok and runtime_results[-1].ok
        assert len(control_flow) == 1 and control_flow[0]["seen_without_intent"] == []
        return
    assert "browser_fill missing required arg 'ref'" in parent.model_text
    assert "recovery did not consume" not in parent.model_text
    assert len(control_flow) == 1 and control_flow[0]["seen_without_intent"] == []
    assert len(control_flow[0]["committed"]) == 2
    assert [r.call.name for r in receipts if r.call.name.startswith("browser_")] == [
        "browser_navigate", "browser_snapshot"]
    child_refs = decode_json_object(parent.canonical)["child_effect_ids"]
    assert isinstance(child_refs, list) and len(child_refs) == 2
    assert all(ref in child_refs for ref in control_flow[0]["committed"])
    assert runtime_results[-1].ok is False
    assert "ref" in runtime_results[-1].error
    assert "recovery did not consume" not in runtime_results[-1].error


@pytest.mark.parametrize("complete", [False, True])
def test_structured_delivery_with_controlled_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, complete: bool,
) -> None:
    code = browser.ROUND.replace("return " + browser.READ + ";", "const body=" + browser.READ + ";")
    code += ("const token=unwrap(await tools.browser_eval({code:'window.__contractToken'}));"
             "const tabs=unwrap(await tools.browser_tab_list({}));return {body,token,tabs};")
    summary = "已加载记录，并读取了当前页面。\n\n" + (
        "净重 173 克，校验码 QUARTZ_851。" if complete else "已完成页面操作。")
    install_http(monkeypatch, [browser.code_action(code), {"tool": "done", "args": {"summary": summary}}])
    result = run_case(tmp_path / "run", "chat", TARGET, explanation_output=True, max_turns=2)
    assert result["stop_reason"] == "done", result
    assert result["facts_match"] is complete
    assert result["passed"] is complete


def test_hybrid_outer_read_receipt_reaches_conflict_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    correct = ("const r=await tools.read_file({path:'result.json'});"
               "return unwrap(await tools.edit({read:r,changes:{amount:173,code:'QUARTZ_851'}}));")
    invalid = correct.replace("read:r,", "read:unwrap(r),")
    install_http(monkeypatch, [browser.code_action(browser.ROUND), browser.code_action(invalid),
        browser.code_action(correct), browser.code_action(correct),
        {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}}])
    result = run_case(tmp_path / "run", "hybrid", TARGET, stale_edit=True,
                      preserve_external_field=True, max_turns=5)
    assert result["passed"] is True, result
    assert result["true_stale_conflicts"] == 1
    assert result["fresh_read_recovery"] is True
    assert result["final_file_correct"] is True
    assert result["conflict_recovery_covered"] is True


def test_executable_stale_ref_call_is_rejected_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = (browser.NAVIGATE + "const s=unwrap(await tools.browser_snapshot({}));"
             "const old=s.elements.find(e=>e.name==='Load record').ref;"
             "unwrap(await tools.browser_fill({ref:s.elements.find(e=>e.name==='Locality').ref,value:'福州'}));"
             "return await tools.browser_click({ref:old});")
    recovered = (browser.LOAD + browser.WAIT + "return " + browser.READ + ";")
    install_http(monkeypatch, [browser.code_action(stale), browser.code_action(recovered),
        {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}}])
    result = run_case(tmp_path / "run", "chat", TARGET, stale_ref=True, max_turns=3)
    assert result["passed"] is True, result
    assert result["failed_stale_refs"] == 1
