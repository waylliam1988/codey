"""Task URL binding through frozen production contracts, Host and receipts."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from codey.codemode import kernel as kernel_module
from codey.codemode.catalog import render_declarations
from codey.codemode.kernel import build_execute_turn_executor
from codey.codemode.replay import digest
from codey.operations.browser.catalog import snapshot_catalog
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel.kernel_execution import execute_turn
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.kernel.turn_contract import TurnSnapshot
from codey.operations.project.project_delivery import ProjectDeliveryLinks
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_handoff import task_handoff
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy, build_task_policy
from codey.protocols.text_turn import decode_text_turn
from codey.research.source_document import OpenedSource
from codey.runtime.core.models import ToolCall, ToolResult
from codey.task.model import TaskSubmission
from codey.task.web_intent import explicit_web_intent, source_requirements, source_url_key
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object, require_json_string
from tests.test_task_contract_fixed_replies import TARGET, install_original_http, saved_replies
from tools.browser_production_live_gate import URL, run_case
from tools.completion_artifact_audit import tool_receipts


def test_project_disclosure_reuses_documented_source_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    session = session_for("Modify app.py using the project specification.", kind="project",
                          grants=GRANTS | {"project.write"})
    session.project = "."
    snapshot = build_turn_snapshot(session)
    selected, _ = task_handoff(session, snapshot, CompletionContext(), None)
    assert "browser_navigate" not in selected.tool_names
    assert "browser_navigate" in selected.nested_tool_names
    assert "task_output" not in selected.tool_names and "task_output" in selected.nested_tool_names
    active, _ = task_handoff(session, snapshot, CompletionContext(), None,
                             task_controls=frozenset({"task_output", "task_cancel"}))
    assert {"task_output", "task_cancel"} <= set(active.tool_names)
    assert "task_input" not in active.tool_names
    url = "https://example.com/project-format"
    monkeypatch.setattr("codey.operations.project.project_delivery.observed_delivery_links",
                        lambda _session: ProjectDeliveryLinks(specification=url))
    selected, _ = task_handoff(session, snapshot, CompletionContext(), None)
    restored = restore_snapshot(snapshot_description(selected))
    assert restored.browser.source_url == url
    assert "open_url" in restored.tool_names


@pytest.mark.parametrize("nested_only", [False, True])
def test_specification_handoff_executes_through_the_frozen_surface(
    monkeypatch: pytest.MonkeyPatch, nested_only: bool,
) -> None:
    session = session_for("Modify app.py using the project specification.", kind="project",
                          grants=GRANTS | {"project.write"})
    session.project = "."
    if nested_only:
        session.model_tool_names = ("codemode", "read_file", "edit")
    url = "https://example.com/project-format"
    monkeypatch.setattr("codey.operations.project.project_delivery.observed_delivery_links",
                        lambda _session: ProjectDeliveryLinks(specification=url))
    selected, hint = task_handoff(session, build_turn_snapshot(session), CompletionContext(), None)
    snapshot = restore_snapshot(snapshot_description(selected))
    assert ("open_url" not in snapshot.tool_names) == nested_only
    assert "open_url" in snapshot.nested_tool_names
    _, marker, next_call = hint.partition("Read the record specification: ")
    assert marker
    suggested = next_call.splitlines()[0]
    assert url not in suggested
    plan = normalize_turn(decode_text_turn(suggested), snapshot=snapshot)
    assert not plan.protocol_error and len(plan.calls) == 1, plan.protocol_error
    assert plan.calls[0].name == ("codemode" if nested_only else "open_url")

    def open_specification(call: ToolCall) -> ToolResult:
        assert call.args == {"url": url}
        body = "Fixture record specification"
        source = OpenedSource(url, url, "Specification", "2026-11-23T00:00:00Z", hashlib.sha256(body.encode()).hexdigest()[:16])
        return ToolResult(call, body, ok=True, canonical={
            "value": body,
            "research_observation": {"source": source.to_dict(), "text": body, "pages": []},
            "read_passages": [{"start": 0, "end": len(body)}],
        })

    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="specification-handoff",
        effect_scope="task", turn=1, executors={"open_url": open_specification})
    result, = execute_turn(session, plan.calls, run_id="specification-handoff", effect_scope="task", turn=1,
        snapshot=snapshot, executors={"codemode": parent, "open_url": open_specification})
    assert result.ok, result.model_text
    assert any(row.ok and row.call.name == "open_url" and row.call.args == {"url": url}
               and row.canonical["value"] == "Fixture record specification" for row in session._memory_results.values())
    assert url in session.source_ids.values()


def test_read_only_project_keeps_discovery_and_omits_unused_outer_tools() -> None:
    session = session_for("Inspect app.py for the current interface.", kind="project")
    session.project = "."
    initial = build_turn_snapshot(session)
    selected, _ = task_handoff(session, initial, CompletionContext(), None)
    assert {"read_file", "codemode"} <= set(selected.tool_names)
    assert "edit" not in selected.inventory_names
    assert "web_search" in selected.nested_tool_names and "web_search" not in selected.tool_names
    codemode = next(spec for spec in selected.model_specs if spec.name == "codemode")
    assert "function describe" in codemode.declarations and "web_search(args:" not in codemode.declarations
GRANTS = frozenset({"control", "project.read", "web.read", "browser.observe", "browser.interact"})
ORIGINAL = "https://EXAMPLE.COM/A-b_C?x=A%2FB&n=0042#Part-1"


def session_for(task: str, *, kind: str = "chat", grants: frozenset[str] = GRANTS) -> TaskSession:
    return TaskSession(TaskPolicy(grants, strict_research=kind == "research"), task_kind=kind,
                       task_text=task, required_source_urls=explicit_web_intent(task).urls)


@pytest.mark.parametrize("kind", ["chat", "hybrid"])
def test_removed_action_contract_requires_new_request(kind: str) -> None:
    submission = TaskSubmission(
        session_id="retired-action", project=None, task="Open the page.", max_turns=4,
        continue_task=False, provider_id="local",
        required_tools=('tool_succeeded:{"args":{},"input":"actions","tool":"codemode","via":""}',),
    )
    with pytest.raises(ValueError, match="invalid tool predicate") as rejected:
        build_task_policy(submission, task_kind=kind)
    assert "codemode.action has been removed" in str(rejected.value.__cause__)

    snapshot = build_turn_snapshot(session_for(f"Open <{ORIGINAL}>.", kind=kind))
    payload = snapshot_description(snapshot)
    payload["input_binding"] = {"kind": "actions", "tool": "codemode", "source_refs": []}
    payload["task_navigation_url"] = None
    with pytest.raises(ValueError, match=r"codemode\.action input is no longer supported"):
        restore_snapshot(payload)


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
    assert snapshot.browser.navigation_url == ORIGINAL


@pytest.mark.parametrize("task", ["查看结果", f"不要打开 {URL}", f"代码示例 `open {URL}`",
    f"```text\nopen {URL}\n```", f"打开 {URL} 和 https://example.com/b",
    f"打开 <{URL}#one> 和 <{URL}#two>", f"打开 <{URL}> 和 <https://EXAMPLE.COM/production-browser-record>",
    "打开 https://127.0.0.1/a", f"打开 {URL}.", f"打开 {URL})", f"打开 <{URL}> 和 {URL}."])
def test_ambiguous_or_unrequested_addresses_have_no_binding(task: str) -> None:
    session = session_for(task)
    snapshot = build_turn_snapshot(session)
    assert snapshot.browser.navigation_url is None
    calls: list[ToolCall] = []
    result = run_program(session, snapshot, calls, "return unwrap(await tools.browser_navigate({target:'task'}));")
    assert not result.ok and not calls


@pytest.mark.parametrize("kind", ["research", "project"])
def test_authorized_task_binding_is_shared_by_research_and_project(kind: str) -> None:
    assert build_turn_snapshot(session_for(f"打开 {URL}", kind=kind)).browser.navigation_url == URL


def test_hybrid_uses_the_original_task_address() -> None:
    assert build_turn_snapshot(session_for(f"打开 <{ORIGINAL}>", kind="hybrid")).browser.navigation_url == ORIGINAL


def test_authorization_and_frozen_required_source_must_match() -> None:
    session = session_for(f"打开 {URL}", grants=GRANTS - {"browser.interact"})
    source_only = build_turn_snapshot(session)
    assert source_only.browser.navigation_url == URL and "browser_navigate" not in source_only.inventory_names
    session = session_for(f"打开 {URL}")
    session.required_source_urls = frozenset({"https://example.com/other"})
    assert build_turn_snapshot(session).browser.navigation_url is None
    session.required_source_urls = frozenset()
    assert build_turn_snapshot(session).browser.navigation_url is None
    session = session_for(f"打开 {URL}")
    session.controller_denied = ("browser_navigate",)
    snapshot = build_turn_snapshot(session)
    assert snapshot.browser.navigation_url == URL
    assert "browser_navigate" not in snapshot.inventory_names
    calls: list[ToolCall] = []
    result = run_program(session, snapshot, calls, "return unwrap(await tools.browser_navigate({target:'task'}));")
    assert not result.ok and not calls


def test_composed_model_exposure_can_use_its_authorized_nested_navigation() -> None:
    session = session_for(f"打开 {URL}")
    session.model_tool_names = ("codemode", "done")
    snapshot = build_turn_snapshot(session)
    assert snapshot.browser.navigation_url == URL
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
    assert snapshot.browser.navigation_url == expected
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
    assert build_turn_snapshot(session_for(task)).browser.navigation_url == URL + "#new"


def test_legacy_source_requirement_cannot_reauthorize_a_negated_task_url() -> None:
    session = session_for("Open the report and do not visit https://example.com/records/Case-A")
    session.required_source_urls = frozenset({"https://example.com/records/Case-A"})
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session)))
    assert snapshot.browser.navigation_url is None
    calls: list[ToolCall] = []
    result = run_program(session, snapshot, calls, "return unwrap(await tools.browser_navigate({target:'task'}));")
    assert not result.ok and not calls


@pytest.mark.parametrize("task", [f"打开 {URL}，上传附件。",
    f"打开 {URL}，把运单号原样写入 dispatch.json 的 carrier_label，保留前导零。"])
def test_dedicated_input_contracts_keep_their_own_binding(task: str) -> None:
    snapshot = build_turn_snapshot(session_for(task, grants=GRANTS | {"project.write", "browser.effect"}))
    assert snapshot.input_binding is not None
    assert snapshot.browser.navigation_url is None


@pytest.mark.parametrize("native", [False, True])
def test_snapshot_roundtrip_and_runtime_use_the_same_bound_catalog(native: bool) -> None:
    session = session_for(f"打开 <{ORIGINAL}>")
    snapshot = build_turn_snapshot(session, native=native)
    restored = restore_snapshot(snapshot_description(snapshot))
    assert restored == snapshot
    catalog = snapshot_catalog(restored)
    specs = {spec.name: spec for spec in restored.frozen_specs}
    assert specs["codemode"].declarations == render_declarations(catalog,
        outer_descriptions={spec.name: spec.description for spec in restored.model_specs})
    assert 'target: "task"' in specs["codemode"].declarations
    assert set(dict(specs["browser_navigate"].parameters)) == {"url"}
    unbound = snapshot_catalog(replace(restored, browser=replace(restored.browser, navigation_url=None)))
    assert catalog.digest != unbound.digest
    assert [entry for entry in catalog.entries if entry.name not in {"browser_navigate", "open_url", "source_search"}] == [
        entry for entry in unbound.entries if entry.name not in {"browser_navigate", "open_url", "source_search"}]
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
    for key in ("browser_fill_text", "browser_followups", "browser_references"):
        del payload[key]
    payload["schema_version"] = 6
    del payload["task_navigation_url"]
    with pytest.raises(ValueError, match="unsupported captured turn snapshot version"):
        restore_snapshot(payload)
    payload = snapshot_description(snapshot)
    payload["unknown"] = None
    with pytest.raises(ValueError, match="shape"):
        restore_snapshot(payload)
    payload = snapshot_description(snapshot)
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
    denied = replace(snapshot, policy=TaskPolicy(GRANTS - {"browser.interact", "web.read"}))
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
    def record_digest(value: object) -> str:
        if isinstance(value, dict):
            row = decode_json_object(value)
            if {"args", "catalog", "runtime", "task_navigation_url"} <= row.keys():
                fingerprints.append(row)
        return digest(value)

    monkeypatch.setattr(kernel_module, "digest", record_digest)
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
    assert len(navigation) == 2
    prerequisite, requested = navigation
    assert prerequisite.call.call_id == "required-source:navigation" and prerequisite.ok
    assert prerequisite.call.args == {"url": URL}
    assert requested.call.args == {"url": URL if bound else URL.replace("-", "_")}
    assert decode_json_object(requested.canonical["value"])["http_status"] == (200 if bound else 404)
    assert all("recovery did not consume" not in row.model_text for row in receipts)
    assert fingerprints and all(row["task_navigation_url"] == URL for row in fingerprints)
