"""The experiment changes visibility while using real production admission and effects."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession
from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn, refresh_snapshot_contract
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import _browser_body
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall, tools_from_specs
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_browser_production_execution import URL
from tests.test_browser_production_execution import production_browser as production_browser
from tests.test_model_input_binding import upload_snapshot
from tools.browser_leaf_exposure_ab import CASES, DIRECT_TOOLS, NOTE, evidence_checks, expose_leaves, metrics, run_arm
from tools.completion_explanation_ab import Target

GRANTS = frozenset({"control", "web.read", "browser.observe", "browser.interact"})


def test_visibility_and_roundtrip_reuse_one_definition_per_leaf() -> None:
    session = TaskSession(TaskPolicy(GRANTS), task_kind="chat")
    a = build_turn_snapshot(session)
    b = expose_leaves(a, session)
    assert not set(DIRECT_TOOLS).intersection(a.tool_names)
    assert set(b.tool_names) - set(a.tool_names) == set(DIRECT_TOOLS)
    assert b.nested_tool_names == a.nested_tool_names and b.policy is a.policy and b.allowed == a.allowed
    original = {spec.name: spec for spec in a.frozen_specs}
    assert all(spec is original[spec.name] for spec in b.frozen_specs)
    assert len(b.frozen_specs) == len(set(b.inventory_names))
    assert {spec.name: spec for spec in b.frozen_specs} == original
    assert dict(b.custom_executors) == dict(a.custom_executors)
    restored = restore_snapshot(snapshot_description(b))
    assert restored == b
    assert set(DIRECT_TOOLS) <= {tool.name for tool in tools_from_specs(restored.model_specs)}
    for name, args in (("browser_snapshot", {}), ("browser_fill", {"ref": "observed", "value": "text"}),
                       ("browser_click", {"ref": "observed"}), ("browser_read", {})):
        reply = AssistantTurn(tool_calls=(ProviderToolCall("identity", name, args),))
        assert normalize_turn(reply, snapshot=a).protocol_error_kind == "snapshot_tool_mismatch"
        accepted = normalize_turn(reply, snapshot=restored)
        assert not accepted.protocol_error and accepted.calls[0].call_id == "identity"
    assert normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("bad", "browser_fill", {"ref": "x"}),)),
                          snapshot=restored).protocol_error
    assert not normalize_turn(decode_text_turn('{"tool":"browser_read","args":{}}'), snapshot=restored).protocol_error


@pytest.mark.parametrize("guard", ["grant", "controller", "exposure", "upload", "copy", "restricted"])
def test_experiment_cannot_broaden_authorization_or_escape_simplified_inputs(guard: str) -> None:
    grants = GRANTS - {"browser.interact"} if guard == "grant" else GRANTS
    session = TaskSession(TaskPolicy(grants), task_kind="chat")
    if guard == "controller":
        session.controller_denied = ("browser_click", "browser_fill")
    if guard == "exposure":
        session.model_tool_names = ("codemode", "done")
    a = upload_snapshot() if guard == "upload" else build_turn_snapshot(session)
    if guard == "copy":
        from tests.test_model_input_binding import copy_snapshot
        a = copy_snapshot()
    if guard == "restricted":
        a = refresh_snapshot_contract(replace(a, allowed=("codemode", "done")))
    b = expose_leaves(a, session)
    if guard in {"grant", "controller"}:
        assert not {"browser_fill", "browser_click"}.intersection(b.tool_names)
        for tool in ("browser_fill", "browser_click"):
            plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("denied", tool,
                {"ref": "old", **({"value": "x"} if tool == "browser_fill" else {})}),)), snapshot=b)
            assert plan.protocol_error and not plan.calls
    else:
        assert b == a


@pytest.mark.parametrize("direct", [False, True])
def test_real_kernel_executes_the_same_fill_click_read_and_rejects_stale_ref(
    tmp_path: Path, production_browser: tuple[TaskSession, ResearchTools, BrowserSession], direct: bool,
) -> None:
    session, tools, browser = production_browser
    assert browser.navigate(URL).ok
    turn = 0

    def call(name: str, args: JsonObject):
        nonlocal turn
        turn += 1
        snapshot = build_turn_snapshot(session)
        if direct:
            snapshot = expose_leaves(snapshot, session)
        snapshot = restore_snapshot(snapshot_description(snapshot))
        executors = {"codemode": build_execute_turn_executor(session=session, snapshot=snapshot,
            run_id="leaf-test", effect_scope="task", turn=turn, project_path=tmp_path, research_tools=tools)}
        action = ToolCall(name, args, call_id=f"outer-{turn}") if direct else ToolCall("codemode", {
            "code": f"return unwrap(await tools.{name}(inputs));", "inputs": args}, call_id=f"outer-{turn}")
        plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall(action.call_id, action.name, action.args),)),
                              snapshot=snapshot)
        assert not plan.protocol_error, plan.protocol_error
        result, = execute_turn(session, plan.calls, snapshot=snapshot, executors=executors,
            research_tools=tools, run_id="leaf-test", effect_scope="task", turn=turn, project_path=tmp_path)
        if direct:
            return result
        refs = result.canonical["child_effect_ids"]
        assert isinstance(refs, list) and len(refs) == 1
        return session._memory_results[str(refs[0])]

    observed = call("browser_snapshot", {})
    elements = decode_json_object(observed.canonical["value"])["elements"]
    assert isinstance(elements, list)
    textbox = next(row["ref"] for row in elements if isinstance(row, dict) and row["role"] == "textbox")
    old_button = next(row["ref"] for row in elements if isinstance(row, dict) and row["role"] == "button")
    assert call("browser_fill", {"ref": textbox, "value": "Fuzhou"}).ok
    refused = call("browser_click", {"ref": old_button})
    assert not refused.ok and decode_json_object(refused.canonical["value"])["delivery"] == "not_delivered"
    observed = call("browser_snapshot", {})
    elements = decode_json_object(observed.canonical["value"])["elements"]
    assert isinstance(elements, list)
    button = next(row["ref"] for row in elements if isinstance(row, dict) and row["role"] == "button")
    clicked = call("browser_click", {"ref": button})
    assert clicked.ok and "read_passages" not in clicked.canonical
    read = call("browser_read", {})
    assert read.ok and "37 grams" in str(_browser_body(read))
    assert "read_passages" in read.canonical
    source = str(read.canonical["source_id"])
    assert evidence_checks([read], f"37 grams [{source}]", ("37 grams",))["correct_evidence_cited"]
    assert not evidence_checks([clicked], f"37 grams [{source}]", ("37 grams",))["facts_in_read_receipts"]
    assert not evidence_checks([read], "37 grams [s999]", ("37 grams",))["correct_evidence_cited"]
    children = [row for row in session._memory_results.values() if row.call.name in DIRECT_TOOLS]
    assert sum(row.call.name == "browser_fill" and row.ok for row in children) == 1
    assert sum(row.call.name == "browser_click" and row.ok for row in children) == 1
    assert sum(row.call.name == "browser_read" and row.ok for row in children) == 1


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("arm", ["A", "B"])
@pytest.mark.parametrize("case_name", ["visible", "quoted"])
def test_full_headless_harness_uses_bound_page_and_real_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str, case_name: str,
) -> None:
    from collections.abc import Iterator

    from tests.test_receipt_bound_edit_surface_probe import install_http
    from tools.completion_artifact_audit import tool_receipts

    directory = tmp_path / arm

    def ref(role: str) -> str:
        observed = [row.result for row in tool_receipts(directory / "state") if row.result.call.name == "browser_snapshot"][-1]
        elements = decode_json_object(observed.canonical["value"])["elements"]
        assert isinstance(elements, list)
        return next(str(row["ref"]) for row in elements if isinstance(row, dict) and row["role"] == role)

    def proposals() -> Iterator[JsonObject]:
        if arm == "A":
            setup = ("const a=unwrap(await tools.browser_snapshot({}));"
                "unwrap(await tools.browser_fill({ref:a.elements.find(e=>e.role==='textbox').ref,value:inputs.note}));"
                "const b=unwrap(await tools.browser_snapshot({}));"
                "unwrap(await tools.browser_click({ref:b.elements.find(e=>e.role==='button').ref}));") if case_name == "quoted" else ""
            yield {"tool": "codemode", "args": {"code": setup + "return unwrap(await tools.browser_read({}));",
                                                 "inputs": {"note": NOTE}}}
        else:
            if case_name == "quoted":
                yield {"tool": "browser_snapshot", "args": {}}
                yield {"tool": "browser_fill", "args": {"ref": ref("textbox"), "value": NOTE}}
                yield {"tool": "browser_snapshot", "args": {}}
                yield {"tool": "browser_click", "args": {"ref": ref("button")}}
            yield {"tool": "browser_read", "args": {}}
        yield {"tool": "done", "args": {"summary": ("备注已受理，回执码 NOTE_624。 [s1]" if case_name == "quoted"
                                                   else "LY-72 可借 6 本，今天 19:40 闭馆。 [s1]")}}

    install_http(monkeypatch, proposals())
    row = run_arm(directory, CASES[case_name], arm, Target("http://scripted.test/v1", "scripted-fixture", 1, 10))
    assert row["passed"], row
    assert row["model_calls"] == (6 if arm == "B" and case_name == "quoted" else 2)
    assert row["source_stable"] and row["bound_before_model"]
    assert row["direct_executions"] == (({"browser_snapshot": 2, "browser_fill": 1, "browser_click": 1,
        "browser_read": 1} if case_name == "quoted" else {"browser_read": 1}) if arm == "B" else {})


def test_metrics_distinguish_attempts_from_executed_actions() -> None:
    row = metrics([], [{"tool": "browser_click", "args": {"ref": "bad"}},
        {"tool": "codemode", "args": {"code": "return undefined;"}}])
    assert row["direct_attempts"] == {name: int(name == "browser_click") for name in DIRECT_TOOLS}
    assert row["direct_executions"] == {} and row["javascript_proposals"] == 1


def test_multiline_fixture_value_is_the_user_authorized_text() -> None:
    assert CASES["quoted"].field_value == NOTE
    assert NOTE in CASES["quoted"].question


@pytest.mark.parametrize("guard", ["grant", "controller", "exposure"])
def test_kernel_rechecks_unavailable_direct_calls_without_parser(
    tmp_path: Path, production_browser: tuple[TaskSession, ResearchTools, BrowserSession], guard: str,
) -> None:
    session, tools, _browser = production_browser
    if guard == "grant":
        session.policy = replace(session.policy, grants=session.policy.grants - {"browser.interact"})
    elif guard == "controller":
        session.controller_denied = ("browser_click",)
    else:
        session.model_tool_names = ("codemode", "done")
    snapshot = expose_leaves(build_turn_snapshot(session), session)
    result, = execute_turn(session, [ToolCall("browser_click", {"ref": "forged"})],
        snapshot=snapshot, research_tools=tools, project_path=tmp_path, run_id="denial", turn=0)
    assert not result.ok and "not available" in result.model_text
    assert not session.executed and not session._memory_results
