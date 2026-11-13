"""Settled browser work survives parent failures without executing it again."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.kernel_contracts import ToolExecutor
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_prompt import _browser_read_context, _result_context
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.operations.tool_result_reader import read_tool_result
from codey.policies.task_policy import TaskPolicy
from codey.research.ledger import OpenedSource
from codey.research.passages import passage_ref
from codey.runtime.core.models import ToolCall, ToolResult
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object
from tests.test_production_navigation_ab import action, fixed_flow
from tests.test_task_contract_fixed_replies import TARGET, install_original_http
from tools.completion_artifact_audit import tool_receipts
from tools.production_navigation_ab import production_cases, read_jsonl, run_arm

pytestmark = pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")


def run_program(session: TaskSession, project: Path, turn: int, code: str, token: str = "", *,
                executors: Mapping[str, ToolExecutor] | None = None) -> ToolResult:
    snapshot = build_turn_snapshot(session)
    parent = build_execute_turn_executor(session=session, snapshot=snapshot, project_path=project,
        run_id="feedback", effect_scope="task", turn=turn, executors=executors)
    args: JsonObject = {"code": code}
    if token:
        args["repair_of"] = token
    return execute_turn(session, [ToolCall("codemode", args, call_id=f"parent-{turn}")],
        snapshot=snapshot, executors={"codemode": parent}, project_path=project,
        run_id="feedback", effect_scope="task", turn=turn)[0]


@pytest.mark.parametrize("index", [2, 3])
def test_failed_parent_delivers_original_browser_read_before_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: int,
) -> None:
    scenario, expected = production_cases()[index]
    actions = fixed_flow(scenario, expected, "B", index)
    args = decode_json_object(actions[2]["args"])
    code = args["code"]
    assert isinstance(code, str)
    # Original observed failure: browser_read succeeds, but its string has no .text.
    args["code"] = code.replace("return unwrap(await tools.browser_read({}));",
                                "return unwrap(await tools.browser_read({})).text;")
    actions[2]["args"] = args
    install_original_http(monkeypatch, [json.dumps(action, ensure_ascii=False) for action in actions])
    directory = tmp_path / "run"
    result = run_arm(directory, scenario, expected, "B", TARGET, max_turns=5)
    assert result["passed"] is True, result
    receipts = [row.result for row in tool_receipts(directory / "state")]
    for name in ("browser_click", "browser_wait", "browser_read"):
        children = [row for row in receipts if row.call.name == name]
        assert len(children) == 1 and children[0].ok
    parent = next(row for row in receipts if row.call.name == "codemode" and not row.ok)
    assert parent.audit["error_code"] in {"missing_return", "result_encoding"}
    read_row = next(row for row in tool_receipts(directory / "state") if row.result.call.name == "browser_read")
    requests = [row for row in read_jsonl(directory / "provider.jsonl") if row.get("phase") == "request"]
    final_request = json.dumps(requests[-1], ensure_ascii=False)
    assert "Successful browser reads from settled child receipts" in final_request
    assert all(fact in final_request for fact in expected.facts)
    assert read_row.identity in final_request
    assert "If the existing task requirements are satisfied" in final_request
    messages = decode_json_object(requests[-1]["payload"])["messages"]
    assert isinstance(messages, list)
    contents = [content for message in messages if isinstance(message, dict)
                and isinstance(content := message.get("content"), str)]
    feedback = next(content for content in contents if "Successful browser reads from settled child receipts" in content)
    raw_reads = feedback.split("Successful browser reads from settled child receipts", 1)[1].split(":\n", 1)[1].split("\n", 1)[0]
    delivered = parse_json_object('{"reads":' + raw_reads + '}')["reads"]
    assert isinstance(delivered, list) and len(delivered) == 1
    original_read = decode_json_object(delivered[0])
    assert original_read["text"] == read_row.result.model_text
    assert original_read["complete_read"] is True and original_read["display_omitted"] is False


@pytest.mark.parametrize("mode", ["arguments", "tool", "early", "caught", "syntax", "valid", "unknown"])
def test_repair_feedback_keeps_first_specific_failure(tmp_path: Path, mode: str) -> None:
    (tmp_path / "a.txt").write_text("ORIGINAL_A", encoding="utf-8")
    (tmp_path / "b.txt").write_text("ORIGINAL_B", encoding="utf-8")
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read"})), project=str(tmp_path))
    prefix = "unwrap(await tools.read_file({path:'a.txt'}));"
    second = "unwrap(await tools.read_file({path:'b.txt'}))"
    failed = run_program(session, tmp_path, 0, prefix + second + ";return undefined;")
    assert not failed.ok
    token = decode_json_object(failed.canonical["repair"])["token"]
    assert isinstance(token, str)
    (tmp_path / "b.txt").write_text("CHANGED_B", encoding="utf-8")
    code = {
        "arguments": prefix + "return unwrap(await tools.read_file({path:'a.txt'}));",
        "tool": prefix + "return unwrap(await tools.list_dir({path:'.'}));",
        "early": prefix + "return 'early';",
        "caught": prefix + "try {await tools.list_dir({path:'.'});} catch {} return 'caught';",
        "syntax": "return (;",
        "valid": prefix + "return " + second + ";",
        "unknown": prefix + "return " + second + ";",
    }[mode]
    result = run_program(session, tmp_path, 1, code, "cmr_unknown" if mode == "unknown" else token)
    if mode == "valid":
        assert result.ok and "ORIGINAL_B" in json.dumps(result.canonical)
    else:
        assert not result.ok
        if mode in {"tool", "caught"}:
            assert "step 2" in result.model_text and "expected read_file" in result.model_text
            assert "received list_dir" in result.model_text
            assert "did not consume" not in result.model_text
        elif mode == "arguments":
            assert "step 2" in result.model_text and '"path":"b.txt"' in result.model_text
            assert '"path":"a.txt"' in result.model_text
            assert "did not consume" not in result.model_text
        elif mode == "early":
            assert "remaining steps: 2: read_file" in result.model_text
        elif mode == "syntax":
            assert "syntax" in result.model_text.lower() and "did not consume" not in result.model_text
        else:
            assert "unknown or expired" in result.model_text
    assert len([row for row in session._memory_results.values() if row.call.name == "read_file"]) == 2


def test_stale_ref_feedback_uses_observed_source_not_a_replacement(tmp_path: Path) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"})), task_kind="chat")
    source = ToolResult(ToolCall("open_result", {"result_id": "r1"}, "open"), "North warehouse", ok=True,
        canonical={"research_observation": {"source": {
            "requested_url": "https://example.com/north", "final_url": "https://example.com/north",
            "title": "North warehouse"}}})
    session._memory_results["source-ref"] = source
    failed = ToolResult(ToolCall("browser_click", {"ref": "invented"}, "click"), "ERROR: stale reference", ok=False,
        canonical={"value": {"reason": "stale_ref", "ref": "invented", "delivery": "not_delivered"}})
    text = _result_context(failed, session)
    assert "r1" in text and "North warehouse" in text and "https://example.com/north" in text
    assert "fresh browser_snapshot" in text and "reselect" in text
    assert session._memory_results["source-ref"] is source


@pytest.mark.parametrize("reference", ["unknown", "expired"])
def test_wrong_source_and_invalid_ref_can_reobserve_and_reselect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reference: str,
) -> None:
    scenario, expected = production_cases()[3]
    wrong_url = scenario.source.documents[2][0]
    documents = tuple((url, '<html><head><meta charset="utf-8"><title>North warehouse</title></head>'
                       '<body><h1>北仓 RX-08</h1><p>This page contains North warehouse information. '
                       'No West warehouse controls are present.</p><button>North only</button></body></html>'
                       if url == wrong_url else body)
                      for url, body in scenario.source.documents)
    scenario = replace(scenario, source=replace(scenario.source, documents=documents))
    original = fixed_flow(scenario, expected, "B", 3)
    observe = "const s=unwrap(await tools.browser_snapshot({}));"
    invalid = (observe + "unwrap(await tools.browser_snapshot({}));return unwrap(await tools.browser_click({ref:s.elements[0].ref}));"
               if reference == "expired" else observe + "return unwrap(await tools.browser_click({ref:'invented'}));")
    missing = action(observe + "if (!s.elements.some(e=>e.name===inputs.name)) throw new Error('Target absent on observed page');"
                     "return s;", {"name": expected.click_name})
    actions = [original[0], action("return unwrap(await tools.open_result({result_id:'r1'}));"),
               action(invalid), missing, *original[1:]]
    install_original_http(monkeypatch, [json.dumps(item, ensure_ascii=False) for item in actions])
    directory = tmp_path / "run"
    result = run_arm(directory, scenario, expected, "B", TARGET, max_turns=8)
    assert result["passed"] is True, result
    requests = [row for row in read_jsonl(directory / "provider.jsonl") if row.get("phase") == "request"]
    after_rejection = json.dumps(requests[3], ensure_ascii=False)
    after_observing = json.dumps(requests[4], ensure_ascii=False)
    for wire in (after_rejection, after_observing):
        assert "Historical source selections" in wire and wrong_url in wire and "North warehouse" in wire
        assert "r1" in wire
    assert "fresh browser_snapshot" in after_rejection and "reselect" in after_rejection
    assert "Target absent on observed page" in after_observing
    receipts = [row.result for row in tool_receipts(directory / "state")]
    opened = [row for row in receipts if row.call.name == "open_result"]
    assert [row.call.args["result_id"] for row in opened] == ["r1", "r2"]
    assert all(row.ok for row in opened)
    rejected = [row for row in receipts if row.call.name == "browser_click" and not row.ok]
    assert len(rejected) == 1
    assert decode_json_object(rejected[0].canonical["value"])["reason"] == "stale_ref"
    north_snapshots = [decode_json_object(row.canonical["value"]) for row in receipts
                       if row.ok and row.call.name == "browser_snapshot"
                       and decode_json_object(row.canonical["value"])["page_url"] == wrong_url]
    assert len(north_snapshots) >= 2
    elements = north_snapshots[-1]["elements"]
    assert isinstance(elements, list)
    assert not any(isinstance(element, dict) and element.get("name") == expected.click_name for element in elements)
    assert len([row for row in receipts if row.call.name == "browser_click" and row.ok]) == 1
    assert len([row for row in receipts if row.call.name == "browser_read"]) == 1


@dataclass
class ReceiptContext:
    session: TaskSession
    managed_outputs: ManagedOutputStore | None = None
    session_id: str = ""
    run_id: str = ""


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("control", [False, True])
def test_large_read_feedback_recovers_original_receipt_without_claiming_missing_text(
    complete: bool, recovery: bool, control: bool,
) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control"} if control else set[str]())), task_kind="chat")
    body = "记录内容\n" * 2000 + "2026-11-18 / 0042091"
    url = "https://example.com/orders/A-173"
    source = OpenedSource(url, url, "Order A-173", "2026-11-13T00:00:00+00:00",
                         hashlib.sha256(body.encode()).hexdigest()[:16], truncated=not complete)
    child = ToolResult(ToolCall("browser_read", {}, "p:child:0"), body, ok=True, truncated=not complete,
        canonical={"research_observation": {"source": source.to_dict(), "text": body, "pages": []},
                   "read_passages": [{"ref": "passage", "start": 0, "end": len(body)}]})
    parent = ToolResult(ToolCall("codemode", {}, "p"), "missing return", ok=False,
        canonical={"child_effect_ids": ["read-ref"]}, audit={"codemode_execution_id": "p"})
    session._memory_results.update({"read-ref": child, "parent-ref": parent})
    text = _browser_read_context(parent, session, receipt_recovery=recovery)
    rows = parse_json_object('{"reads":' + text.split(":\n", 1)[1].split("\n", 1)[0] + '}')["reads"]
    assert isinstance(rows, list) and len(rows) == 1
    row = decode_json_object(rows[0])
    assert row["complete_read"] is complete and row["display_omitted"] is True
    assert row["text"] == child.model_text[:8000]
    if recovery and control:
        args = decode_json_object(row["read_tool_result"])
        assert args == {"result_ref": "read-ref", "view": "output", "offset": 8000, "limit": 4000}
        restored = read_tool_result(ReceiptContext(session), ToolCall("read_tool_result", args))
        assert restored.ok
        data = parse_json_object(restored.model_text)
        assert data["text"] == child.model_text[8000:12000]
    else:
        assert "read_tool_result" not in row
    session._memory_results.pop("parent-ref")
    assert _browser_read_context(parent, session, receipt_recovery=True) == ""


@pytest.mark.parametrize("mode", ["arguments", "tool", "early", "unknown", "valid"])
def test_browser_repair_never_dispatches_new_actions(tmp_path: Path, mode: str) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "browser.observe", "browser.interact"})),
                          task_kind="chat", project=str(tmp_path))
    calls: list[ToolCall] = []

    def read(call: ToolCall) -> ToolResult:
        assert call.name == "browser_read"
        calls.append(call)
        text = f"ORIGINAL_BROWSER_{len(calls)}"
        url = "https://example.com/browser-record"
        text_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        source = OpenedSource(url, url, "Browser record", "2026-11-13T00:00:00+00:00", text_hash)
        return ToolResult(call, text, ok=True, canonical={
            "research_observation": {"source": source.to_dict(), "text": text, "pages": []},
            "read_passages": [{"ref": passage_ref(url, text_hash, 0, len(text)), "start": 0, "end": len(text)}]})

    executors: dict[str, ToolExecutor] = {"browser_read": read, "browser_snapshot": read}
    prefix = "unwrap(await tools.browser_read({limit:500}));"
    failed = run_program(session, tmp_path, 0, prefix + "await tools.browser_read({limit:600});return undefined;",
                         executors=executors)
    token = decode_json_object(failed.canonical["repair"])["token"]
    assert isinstance(token, str) and len(calls) == 2
    code = {"arguments": prefix + "return await tools.browser_read({limit:700});",
            "tool": prefix + "return await tools.browser_snapshot({});",
            "early": prefix + "return null;",
            "unknown": prefix + "return await tools.browser_read({limit:600});",
            "valid": prefix + "return await tools.browser_read({limit:600});"}[mode]
    result = run_program(session, tmp_path, 1, code, "cmr_unknown" if mode == "unknown" else token,
                         executors=executors)
    assert result.ok is (mode == "valid")
    assert len(calls) == 2
    if mode in {"arguments", "tool"}:
        assert "step 2" in result.model_text and "did not consume" not in result.model_text


@pytest.mark.parametrize("display_chars", [200, 9000])
def test_managed_read_recovery_uses_retained_output_offsets(tmp_path: Path, display_chars: int) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control"})), task_kind="chat")
    store = ManagedOutputStore(tmp_path)
    original = "ORIGINAL_START\n" + "retained text\n" * 1000
    retained = store.write_tool_output(session_id="s", run_id="r", tool_id="read-ref", permission_profile="coding_writer",
        tool_name="browser_read", display_ref="read-ref", text=original)
    assert retained is not None
    child = ToolResult(ToolCall("browser_read", {}, "p:child:0"), "WINDOW_MIDDLE\n" + "x" * display_chars,
        ok=True, truncated=True, audit={"managed_output": {"handle": retained.handle, "sha256": retained.sha256,
            "original_bytes": retained.original_bytes, "stored_bytes": retained.stored_bytes,
            "stored_truncated": retained.stored_truncated}})
    parent = ToolResult(ToolCall("codemode", {}, "p"), "missing return", ok=False,
        canonical={"child_effect_ids": ["read-ref"]}, audit={"codemode_execution_id": "p"})
    session._memory_results.update({"read-ref": child, "parent-ref": parent})
    feedback = _browser_read_context(parent, session, receipt_recovery=True)
    rows = parse_json_object('{"reads":' + feedback.split(":\n", 1)[1].split("\n", 1)[0] + '}')["reads"]
    assert isinstance(rows, list)
    data = decode_json_object(rows[0])
    args = decode_json_object(data["read_tool_result"])
    assert args["offset"] == 0
    result = read_tool_result(ReceiptContext(session, store, "s", "r"), ToolCall("read_tool_result", args))
    assert result.ok and parse_json_object(result.model_text)["text"] == original[:4000]
    assert data["complete_read"] is False
