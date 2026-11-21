"""Only proven runtime reference churn is folded; recovery uses existing inputs."""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from codey.operations.browser_task_handoff import bind_browser_references, observation_recovery_handoff
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_progress import KernelProgress
from codey.operations.kernel_protocol import build_turn_snapshot, offer_browser_tools
from codey.operations.progress_comparison import progress_comparison
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.core.progress import ProgressSnapshot
from codey.types.json_value import JsonObject, JsonValue, parse_json_object
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools.browser_production_live_gate import run_case
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target

DOCUMENT: JsonObject = {"url": "https://example.com/record", "target_id": "tab", "loader_id": "document"}
GRANTS = frozenset({"control", "web.read", "browser.observe", "browser.interact", "browser.effect", "project.read", "project.write"})
PROGRAM = "const s=unwrap(await tools.browser_snapshot({}));return s.elements[0].ref;"


def session_for() -> TaskSession:
    return TaskSession(TaskPolicy(GRANTS, required_checks=(require_tool_success("browser_fill"),)),
                       task_kind="chat", task_text="Fill Locality with Fuzhou, then load the record.")


def observe(session: TaskSession, index: int, *, text: str = "Not loaded", field: str = "",
            result_value: JsonValue = None, code: str = PROGRAM,
            document: JsonObject = DOCUMENT) -> ToolResult:
    parent = f"program-{index}"
    token = f"b{'a' * 32}g{index}e1"
    elements: list[JsonValue] = [
        {"role": "textbox", "name": "Locality", "ref": token, "context_id": "", "disabled": False,
         "value": field, "checked": False},
        {"role": "button", "name": "Load record", "ref": token + "button", "context_id": "",
         "disabled": False, "value": None, "checked": None}]
    child = ToolResult(ToolCall("browser_snapshot", {}, f"{parent}:child:0"), "snapshot", ok=True,
        canonical={"value": {"generation": index, "page_id": "page", "page_url": document["url"],
            "complete": True, "elements_truncated": False, "text_truncated": False, "text": text,
            "contexts": [], "elements": elements}}, audit={"browser_document": document})
    ref = f"receipt-{index}"
    session._memory_results[ref] = child
    project_execution_record(session, ref, child.call, child, ok=True)
    value = result_value if result_value is not None else f"Found Locality: {token}; Load Record: "
    output: JsonObject = {"ok": True, "value": value}
    return ToolResult(ToolCall("codemode", {"code": code, "inputs": {}}), json.dumps(output),
        ok=True, canonical={"value": value, "child_effect_ids": [ref]},
        audit={"codemode_execution_id": parent, "child_calls": 1})


def test_reference_churn_warns_before_stopping_and_redelivery_preserves_the_budget() -> None:
    session = session_for()
    progress = KernelProgress()
    for index in range(3):
        result = observe(session, index)
        assert progress_comparison(result, session._memory_results).browser_observation
        progress.observe([result], session, batch_id=f"batch-{index}")
        if index == 1:
            assert not progress.stopped and progress.repeat_feedback()
        saved = ProgressSnapshot.from_payload(parse_json_object(json.dumps(progress.snapshot().to_payload())))
        progress = KernelProgress(snapshot=saved)
        progress.observe([result], session, batch_id=f"batch-{index}")
        assert progress.snapshot() == saved
    assert progress.stopped and progress.idle == 2


@pytest.mark.parametrize("change", ["body", "control", "document", "number", "calculation", "substring", "key", "placeholder"])
def test_real_observations_and_computations_remain_progress(change: str) -> None:
    session = session_for()
    first = observe(session, 0)
    second = observe(session, 1, text="Loaded new information" if change == "body" else "Not loaded",
                     field="Fuzhou" if change == "control" else "")
    if change == "document":
        second = observe(session, 1, document={**DOCUMENT, "loader_id": "different"})
    if change in {"number", "calculation", "substring", "key", "placeholder"}:
        first_token, second_token = f"b{'a' * 32}g0e1", f"b{'a' * 32}g1e1"
        values: tuple[JsonValue, JsonValue]
        if change == "number":
            values = (0, 1)
        elif change == "calculation":
            values = ("computed A", "computed B")
        elif change == "substring":
            values = ("prefix" + first_token, "prefix" + second_token)
        elif change == "placeholder":
            values = (first_token, "<observed-control:0:0>")
        else:
            values = ({first_token: "business"}, {second_token: "business"})
        first = replace(first, canonical={**first.canonical, "value": values[0]})
        second = replace(second, canonical={**second.canonical, "value": values[1]})
    one = progress_comparison(first, session._memory_results)
    two = progress_comparison(second, session._memory_results)
    assert one.information != two.information
    progress = KernelProgress()
    progress.observe([first], session)
    progress.observe([second], session)
    assert progress.idle == 0


def test_returning_to_an_earlier_observed_page_state_is_still_a_transition() -> None:
    session = session_for()
    progress = KernelProgress()
    for index, field in enumerate(("", "Fuzhou", "")):
        progress.observe([observe(session, index, field=field)], session)
        assert progress.idle == 0


@pytest.mark.parametrize("state", ["missing", "extra", "effect", "uncertain", "failed", "partial", "literal", "count", "document"])
def test_incomplete_or_effectful_programs_keep_their_general_return_semantics(state: str) -> None:
    session = session_for()
    parent = observe(session, 0)
    child = session._memory_results["receipt-0"]
    if state == "missing":
        session._memory_results.clear()
    elif state == "extra":
        session._memory_results["extra"] = replace(child, call=replace(child.call, call_id="program-0:child:1"))
    elif state in {"effect", "uncertain", "failed"}:
        child = replace(child, call=replace(child.call, name="browser_click"), ok=state == "effect",
                        canonical={"value": {"delivery": "uncertain" if state == "uncertain" else "delivered"}})
        session._memory_results["receipt-0"] = child
    elif state == "partial":
        session._memory_results["receipt-0"] = replace(child, truncated=True)
    elif state == "document":
        session._memory_results["receipt-0"] = replace(child, audit={"browser_document": {}})
    elif state == "literal":
        parent = replace(parent, call=replace(parent.call, args={"code": PROGRAM, "inputs": {"literal": f"b{'a' * 32}g0e1"}}))
    else:
        parent = replace(parent, audit={**parent.audit, "child_calls": 2})
    comparison = progress_comparison(parent, session._memory_results)
    assert not comparison.browser_observation and comparison.information == parent.model_text


def test_changed_read_windows_and_newly_read_text_cannot_be_hidden_by_ref_normalization() -> None:
    session = session_for()
    comparisons: list[str] = []
    for index, (text, start, end) in enumerate((("AAAABBBB", 0, 4), ("AAAACCCC", 0, 4), ("AAAABBBB", 4, 8), ("CCCCBBBB", 0, 4))):
        parent = observe(session, index)
        identity = f"read-{index}"
        ledger = ResearchLedger()
        url = str(DOCUMENT["url"])
        ledger.record_open(url, url, "record", text)
        session._memory_results[identity] = ToolResult(ToolCall("browser_read", {"offset": start}, f"program-{index}:child:1"),
            "read", ok=True, canonical={"research_observation": ledger_observation(ledger, "open", url=url),
                "read_passages": [{"ref": identity, "start": start, "end": end}]}, audit={"browser_document": DOCUMENT})
        parent = replace(parent, canonical={**parent.canonical, "child_effect_ids": [f"receipt-{index}", identity]},
                         audit={**parent.audit, "child_calls": 2})
        comparisons.append(progress_comparison(parent, session._memory_results).information)
    assert comparisons[0] == comparisons[1]
    assert comparisons[0] != comparisons[2] and comparisons[0] != comparisons[3]


@pytest.mark.parametrize("state", ["ready", "filled", "uncertain", "upload", "ambiguous", "denied", "stale"])
def test_recovery_offers_current_unique_choices_and_preserves_settled_actions(state: str) -> None:
    session = session_for()
    observe(session, 0)
    context = CompletionContext(browser_document=DOCUMENT)
    if state in {"filled", "uncertain", "upload"}:
        action = ToolResult(ToolCall("browser_upload" if state == "upload" else "browser_fill", {}, "fill"), "fill", ok=state == "filled",
            canonical={"value": {"delivery": "verified" if state == "filled" else "uncertain"}},
            audit={"browser_document": DOCUMENT})
        session._memory_results["fill"] = action
        project_execution_record(session, "fill", action.call, action, ok=action.ok)
    if state == "ambiguous":
        child = session._memory_results["receipt-0"]
        value = dict(parse_json_object(json.dumps(child.canonical["value"])))
        elements = value["elements"]
        assert isinstance(elements, list) and isinstance(elements[0], dict)
        elements.append({**elements[0], "ref": "other"})
        session._memory_results["receipt-0"] = replace(child, canonical={"value": value})
    if state == "denied":
        session.controller_denied = ("browser_fill",)
    snapshot = bind_browser_references(session, offer_browser_tools(build_turn_snapshot(session), session), context,
                                       1 if state == "stale" else 0)
    snapshot = restore_snapshot(snapshot_description(snapshot))
    _, hint = observation_recovery_handoff(session, snapshot, context)
    assert bool(hint) is (state == "ready")
    if hint:
        assert '"role": "textbox", "name": "Locality"' in hint and '"ref": {' in hint
        assert "supply value" in hint and "Another snapshot is unnecessary" in hint


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_production_repeated_program_receives_recovery_then_completes_without_another_search_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "production"
    bad = ("const s=unwrap(await tools.browser_snapshot({}));let field='';let button='';"
           "for(const e of s.elements){if(e.name==='Locality'){field=e.ref;break;}"
           "if(e.name==='Load record'){button=e.ref;break;}}return {field,button};")

    def replies() -> Iterator[JsonObject]:
        for _ in range(2):
            yield {"tool": "codemode", "args": {"code": bad, "inputs": {}}}
        yield {"tool": "codemode", "args": {"code": "const a=unwrap(await tools.browser_fill(inputs));"
            "unwrap(await tools.browser_snapshot({}));return a;", "inputs": {
                "ref": {"role": "textbox", "name": "Locality"}, "value": "福州"}}}
        wire = (directory / "provider.jsonl").read_text(encoding="utf-8")
        assert "Repeated observation returned no new page information" in wire and "browser_fill still has no successful" in wire
        yield {"tool": "codemode", "args": {"code": "unwrap(await tools.browser_click(inputs));"
            "unwrap(await tools.browser_wait({condition:'text',value:'鱼丸'}));"
            "return unwrap(await tools.browser_read({}));", "inputs": {"ref": {"role": "button", "name": "Load record"}}}}
        yield {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}}

    install_http(monkeypatch, replies())
    result = run_case(directory, "chat", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 15),
                      codemode_only=True, max_turns=5)
    assert result["passed"], result
    receipts = tool_receipts(directory / "state")
    for name in ("browser_fill", "browser_click", "browser_navigate", "browser_read"):
        assert sum(row.result.call.name == name for row in receipts) == 1
