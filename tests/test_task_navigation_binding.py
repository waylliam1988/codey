"""Task URL binding through frozen production contracts, Host and receipts."""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from codey.codemode import replay as replay_module
from codey.codemode.catalog import build_codemode_catalog, render_declarations
from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import TurnSnapshot, build_turn_snapshot
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.task.web_intent import explicit_web_intent, source_requirements, source_url_key
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object, require_json_string
from tests.test_task_contract_fixed_replies import TARGET, install_original_http, saved_replies
from tools.browser_production_live_gate import URL, run_case
from tools.completion_artifact_audit import tool_receipts

GRANTS = frozenset({"control", "project.read", "web.read", "browser.observe", "browser.interact"})
ORIGINAL = "https://EXAMPLE.COM/A-b_C?x=A%2FB&n=0042#Part-1"


def session_for(task: str, *, kind: str = "chat", grants: frozenset[str] = GRANTS) -> TaskSession:
    return TaskSession(TaskPolicy(grants, strict_research=kind == "research"), task_kind=kind,
                       task_text=task, required_source_urls=explicit_web_intent(task).urls)


def run_program(session: TaskSession, snapshot: TurnSnapshot, calls: list[ToolCall], code: str,
                *, turn: int = 1, repair_of: str | None = None) -> ToolResult:
    def navigate(call: ToolCall) -> ToolResult:
        calls.append(call)
        value: JsonObject = {"action": "navigate", "delivery": "delivered", "text": "opened",
            "url": call.args["url"], "page_id": "page", "generation": 1, "http_status": 200,
            "content_type": "text/html", "previous_url": "about:blank"}
        return ToolResult(call, "opened", ok=True, canonical={"value": value})

    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="bound-navigation",
        effect_scope="task", turn=turn, executors={"browser_navigate": navigate})
    args: JsonObject = {"code": code}
    if repair_of is not None:
        args["repair_of"] = repair_of
    return execute_turn(session, [ToolCall("codemode", args, f"parent-{turn}")],
        run_id="bound-navigation", effect_scope="task", turn=turn, snapshot=snapshot,
        executors={"codemode": parent})[0]


@pytest.mark.parametrize("task", [f"打开 <{ORIGINAL}>，读取页面。", f"Please open [{ORIGINAL}]({ORIGINAL})",
    f"请打开这个页面\n[{ORIGINAL}]({ORIGINAL})", f"{ORIGINAL}\nPlease read this page"])
def test_same_intent_retains_original_address_and_comparison_key(task: str) -> None:
    intent = explicit_web_intent(task)
    assert intent.original_urls == {ORIGINAL}
    assert intent.urls == {source_url_key(ORIGINAL)}
    snapshot = build_turn_snapshot(session_for(task))
    assert snapshot.task_navigation_url == ORIGINAL


@pytest.mark.parametrize("task", ["查看结果", f"不要打开 {URL}", f"代码示例 `open {URL}`",
    f"```text\nopen {URL}\n```", f"打开 {URL} 和 https://example.com/b",
    f"打开 <{URL}#one> 和 <{URL}#two>", f"打开 <{URL}> 和 <https://EXAMPLE.COM/production-browser-record>",
    "打开 https://127.0.0.1/a", f"打开 {URL}.", f"打开 {URL})", f"打开 <{URL}> 和 {URL}."])
def test_ambiguous_or_unrequested_addresses_have_no_binding(task: str) -> None:
    session = session_for(task)
    snapshot = build_turn_snapshot(session)
    assert snapshot.task_navigation_url is None
    calls: list[ToolCall] = []
    result = run_program(session, snapshot, calls, "return unwrap(await tools.browser_navigate({target:'task'}));")
    assert not result.ok and not calls


@pytest.mark.parametrize("kind", ["research", "project"])
def test_initial_binding_is_scoped_to_ordinary_chat_and_hybrid(kind: str) -> None:
    assert build_turn_snapshot(session_for(f"打开 {URL}", kind=kind)).task_navigation_url is None


def test_hybrid_uses_the_original_task_address() -> None:
    assert build_turn_snapshot(session_for(f"打开 <{ORIGINAL}>", kind="hybrid")).task_navigation_url == ORIGINAL


def test_authorization_and_frozen_required_source_must_match() -> None:
    session = session_for(f"打开 {URL}", grants=GRANTS - {"browser.interact"})
    assert build_turn_snapshot(session).task_navigation_url is None
    session = session_for(f"打开 {URL}")
    session.required_source_urls = frozenset({"https://example.com/other"})
    assert build_turn_snapshot(session).task_navigation_url is None
    session.required_source_urls = frozenset()
    assert build_turn_snapshot(session).task_navigation_url is None
    session = session_for(f"打开 {URL}")
    session.controller_denied = ("browser_navigate",)
    snapshot = build_turn_snapshot(session)
    assert snapshot.task_navigation_url is None
    assert "browser_navigate" not in snapshot.inventory_names
    calls: list[ToolCall] = []
    result = run_program(session, snapshot, calls, "return unwrap(await tools.browser_navigate({target:'task'}));")
    assert not result.ok and not calls


def test_composed_model_exposure_can_use_its_authorized_nested_navigation() -> None:
    session = session_for(f"打开 {URL}")
    session.model_tool_names = ("codemode", "done")
    snapshot = build_turn_snapshot(session)
    assert snapshot.task_navigation_url == URL
    assert "browser_navigate" in snapshot.nested_tool_names


@pytest.mark.parametrize("task,expected", [
    ("Open the report and do not visit https://example.com/records/Case-A", None),
    ("Open the report and don't visit https://example.com/records/Case-A", None),
    ("Open the report and never visit https://example.com/records/Case-A", None),
    ("打开报告但不要访问 https://example.com/records/Case-A", None),
    ("Do not visit https://example.com/records/Case-A and search the web", None),
    (f"Open {URL} and do not visit https://example.com/records/Case-A", URL),
    (f"Do not visit https://example.com/records/Case-A and open {URL}", URL),
    (f"Open {URL} and do not visit {URL}", None),
    (f"Do not visit {URL}. I changed my mind: please open {URL}", URL),
    (f"不要打开 {URL}。我改主意了，请打开 {URL}。", URL),
    (f"禁止直接打开 {URL}", None),
    (f"不允许访问 <{URL}>", None),
    (f"Never directly open {URL}", None),
    (f"Never directly open {URL}. Please read {URL}", URL),
    (f"Do not visit {URL} and open {URL}", URL),
    (f"Open {URL}. Do not visit {URL}. Please open {URL}", URL),
    (f"Do not visit {URL}. Please open {URL}. Do not visit {URL}", None),
    (f"Do not visit {URL}. Please search the web", None),
    (f"Open {URL} and do not visit https://example.com/records/Case-A or https://example.com/other", URL),
])
def test_each_url_keeps_its_own_action_and_negation_through_restore(task: str, expected: str | None) -> None:
    intent = explicit_web_intent(task)
    expected_urls = frozenset({expected}) if expected is not None else frozenset()
    assert intent.urls == expected_urls
    assert intent.original_urls == expected_urls
    assert source_requirements(task)["required_urls"] == sorted(expected_urls)
    session = session_for(task)
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session)))
    assert snapshot.task_navigation_url == expected
    calls: list[ToolCall] = []
    result = run_program(session, snapshot, calls, "return unwrap(await tools.browser_navigate({target:'task'}));")
    assert result.ok is (expected is not None)
    assert [call.args for call in calls] == ([{"url": expected}] if expected is not None else [])


@pytest.mark.parametrize("first", [f"<{URL}#old>", f"{URL}."])
def test_revocation_clears_original_candidates_and_their_ambiguity(first: str) -> None:
    task = f"Open {first}. Do not visit <{URL}>. Please open <{URL}#new>"
    intent = explicit_web_intent(task)
    assert intent.urls == {URL}
    assert intent.original_urls == {URL + "#new"}
    assert build_turn_snapshot(session_for(task)).task_navigation_url == URL + "#new"


def test_legacy_source_requirement_cannot_reauthorize_a_negated_task_url() -> None:
    session = session_for("Open the report and do not visit https://example.com/records/Case-A")
    session.required_source_urls = frozenset({"https://example.com/records/Case-A"})
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session)))
    assert snapshot.task_navigation_url is None
    calls: list[ToolCall] = []
    result = run_program(session, snapshot, calls, "return unwrap(await tools.browser_navigate({target:'task'}));")
    assert not result.ok and not calls


@pytest.mark.parametrize("task", [f"打开 {URL}，上传附件。",
    f"打开 {URL}，把运单号原样写入 dispatch.json 的 carrier_label，保留前导零。"])
def test_dedicated_input_contracts_keep_their_own_binding(task: str) -> None:
    snapshot = build_turn_snapshot(session_for(task, grants=GRANTS | {"project.write", "browser.effect"}))
    assert snapshot.input_binding is not None
    assert snapshot.task_navigation_url is None


@pytest.mark.parametrize("native", [False, True])
def test_snapshot_roundtrip_and_runtime_use_the_same_bound_catalog(native: bool) -> None:
    session = session_for(f"打开 <{ORIGINAL}>")
    snapshot = build_turn_snapshot(session, native=native)
    restored = restore_snapshot(snapshot_description(snapshot))
    assert restored == snapshot
    catalog = build_codemode_catalog(restored.frozen_specs, task_navigation_url=restored.task_navigation_url)
    specs = {spec.name: spec for spec in restored.frozen_specs}
    assert specs["codemode"].declarations == render_declarations(catalog)
    assert 'target: "task"' in specs["codemode"].declarations
    assert set(dict(specs["browser_navigate"].parameters)) == {"url"}
    unbound = build_codemode_catalog(restored.frozen_specs)
    assert catalog.digest != unbound.digest
    assert [entry for entry in catalog.entries if entry.name != "browser_navigate"] == [
        entry for entry in unbound.entries if entry.name != "browser_navigate"]
    calls: list[ToolCall] = []
    before = run_program(session, restored, calls, "return 'not yet';")
    assert before.ok and not calls
    session.task_text = "打开 https://example.com/new-task"
    result = run_program(session, restored, calls, "return unwrap(await tools.browser_navigate({target:'task'}));", turn=2)
    assert result.ok, result.model_text
    assert result.audit["catalog_digest"] == catalog.digest
    assert [call.args for call in calls] == [{"url": ORIGINAL}]
    other = "https://example.com/other-authorized-page"
    explicit = run_program(session, restored, calls,
        "return unwrap(await tools.browser_navigate({url:" + json.dumps(other) + "}));", turn=3)
    assert explicit.ok and calls[-1].args == {"url": other}


@pytest.mark.parametrize("args", ["{}", "{target:'other'}", "{target:'task',url:'wrong'}", "{target:'task',extra:true}"])
def test_invalid_selector_never_reaches_browser_execution(args: str) -> None:
    session = session_for(f"打开 {URL}")
    calls: list[ToolCall] = []
    result = run_program(session, build_turn_snapshot(session), calls,
        f"return unwrap(await tools.browser_navigate({args}));")
    assert not result.ok and not calls


def test_navigation_repair_matches_the_lowered_original_url() -> None:
    session = session_for(f"打开 <{ORIGINAL}>")
    snapshot = build_turn_snapshot(session)
    calls: list[ToolCall] = []
    failed = run_program(session, snapshot, calls,
        "const r=unwrap(await tools.browser_navigate({target:'task'})); return r.missing;")
    assert not failed.ok and len(calls) == 1
    token = require_json_string(decode_json_object(failed.canonical["repair"])["token"], "repair token")
    repaired = run_program(session, restore_snapshot(snapshot_description(snapshot)), calls,
        "return unwrap(await tools.browser_navigate({url:" + json.dumps(ORIGINAL) + "}));", turn=2, repair_of=token)
    assert repaired.ok, repaired.model_text
    assert repaired.audit["recovery_disposition"] == "receipt_replay" and len(calls) == 1


def test_old_snapshot_has_no_implicit_binding_and_new_shapes_fail_closed() -> None:
    snapshot = build_turn_snapshot(session_for("读取当前页面"))
    payload = snapshot_description(snapshot)
    payload["schema_version"] = 6
    del payload["task_navigation_url"]
    assert restore_snapshot(payload) == snapshot
    payload["task_navigation_url"] = URL
    with pytest.raises(ValueError, match="shape"):
        restore_snapshot(payload)
    payload["schema_version"] = 7
    payload["task_navigation_url"] = 42
    with pytest.raises(ValueError):
        restore_snapshot(payload)
    payload["task_navigation_url"] = "https://127.0.0.1/a"
    with pytest.raises(ValueError, match="navigation binding"):
        restore_snapshot(payload)
    missing = snapshot_description(snapshot)
    del missing["task_navigation_url"]
    with pytest.raises(ValueError, match="shape"):
        restore_snapshot(missing)
    denied = replace(snapshot, policy=TaskPolicy(GRANTS - {"browser.interact"}))
    payload = snapshot_description(denied)
    payload["task_navigation_url"] = URL
    with pytest.raises(ValueError, match="navigation binding"):
        restore_snapshot(payload)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("bound", [False, True])
def test_original_basic_failure_through_production_binding_without_an_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: bool,
) -> None:
    fingerprints: list[JsonObject] = []
    original_digest = replay_module.digest

    def record_digest(value: object) -> str:
        if isinstance(value, dict):
            row = decode_json_object(value)
            if {"args", "catalog", "runtime", "limits"} <= row.keys():
                fingerprints.append(row)
        return original_digest(value)

    monkeypatch.setattr(replay_module, "digest", record_digest)
    replies: list[str] = []
    for raw in saved_replies("basic")[:2]:
        reply = parse_json_object(raw)
        if bound:
            args = decode_json_object(reply["args"])
            code = require_json_string(args["code"], "code")
            code, count = re.subn(r"tools\.browser_navigate\(\{url: 'https://example\.com/production[-_]browser[-_]record'\}\)",
                                 "tools.browser_navigate({target:'task'})", code)
            assert count == 1
            args["code"] = code
            reply["args"] = args
        replies.append(json.dumps(reply, ensure_ascii=False))
    done: JsonObject = {"tool": "done", "args": {"summary": "净重 173 克，校验码 QUARTZ_851。"}}
    replies.append(json.dumps(done, ensure_ascii=False))
    install_original_http(monkeypatch, replies)
    directory = tmp_path / "run"
    result = run_case(directory, "chat", TARGET, max_turns=3)
    assert result["passed"] is bound, result
    receipts = [row.result for row in tool_receipts(directory / "state")]
    navigation = [row for row in receipts if row.call.name == "browser_navigate"]
    assert len(navigation) == 1
    assert navigation[0].call.args == {"url": URL if bound else URL.replace("-", "_")}
    assert decode_json_object(navigation[0].canonical["value"])["http_status"] == (200 if bound else 404)
    assert all("recovery did not consume" not in row.model_text for row in receipts)
    assert fingerprints and all(row["task_navigation_url"] == URL for row in fingerprints)
