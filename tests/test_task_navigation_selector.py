"""Direct task navigation selects a frozen source instead of transcribing it."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.codemode.navigation import resolve_task_navigation
from codey.knowledge.model import canonical_json
from codey.operations.browser.browser_task_handoff import handoff_call
from codey.operations.browser.catalog import snapshot_catalog
from codey.operations.browser.turn_surface import offer_browser_call
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.kernel.turn_contract import TurnSnapshot
from codey.operations.provider_receipts import TURN_SNAPSHOT_SCHEMA, restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import ToolCall
from codey.task.web_intent import source_url_key
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object
from tests.test_research_submission_handoff_production import NativeExperimentProvider, install_http, requests
from tools import browser_production_live_gate as gate
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target

GRANTS = frozenset({"control", "web.read", "browser.observe", "browser.interact"})
URL = "https://example.com/production-browser-record"
WRONG = "https://example.com/production_browser_record"


def offered(url: str = URL) -> tuple[TaskSession, TurnSnapshot]:
    session = TaskSession(TaskPolicy(GRANTS), task_text=f"请打开 {url}。", task_kind="chat",
        required_source_urls=frozenset({source_url_key(url)}))
    return session, offer_browser_call(build_turn_snapshot(session), session, "browser_navigate")


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("url", [URL, URL + "?id=A%2FB+Q&token=X_47#record"])
def test_direct_selector_freezes_the_exact_source_and_preserves_other_navigation(native: bool, url: str) -> None:
    session, snapshot = offered(url)
    payload = snapshot_description(snapshot)
    assert payload["schema_version"] == TURN_SNAPSHOT_SCHEMA
    restored = restore_snapshot(parse_json_object(canonical_json(payload)))
    assert restored == snapshot
    spec = next(spec for spec in restored.model_specs if spec.name == "browser_navigate")
    assert spec.required == ("target",) and set(dict(spec.parameters)) == {"target"}
    reply = (AssistantTurn(tool_calls=(ProviderToolCall("navigate", "browser_navigate", {"target": "task"}),)) if native
             else decode_text_turn('{"tool":"browser_navigate","args":{"target":"task"}}'))
    plan = normalize_turn(reply, snapshot=restored)
    assert not plan.protocol_error and len(plan.calls) == 1
    assert plan.calls[0].name == "codemode" and plan.calls[0].args["inputs"] == {"target": "task"}
    assert resolve_task_navigation({"target": "task"}, restored.task_navigation_url) == {"url": url}
    alternate = "https://example.com/another-authorized-page"
    assert resolve_task_navigation({"url": alternate}, url) == {"url": alternate}
    _, action, error = handoff_call(session, restored, ToolCall("browser_navigate", {"url": alternate}))
    assert not error and action is not None and action["tool"] == "codemode"
    assert decode_json_object(action["args"])["inputs"] == {"url": alternate}
    assert set(restored.inventory_names) == set(snapshot.inventory_names)


@pytest.mark.parametrize("args", [{"url": WRONG}, {"url": URL}, {"target": "invented"},
    {"target": "task", "url": WRONG}, {}])
def test_raw_url_and_ambiguous_arguments_are_rejected_before_an_effect(args: JsonObject) -> None:
    _, snapshot = offered()
    plan = normalize_turn(decode_text_turn(canonical_json({"tool": "browser_navigate", "args": args})), snapshot=snapshot)
    assert plan.protocol_error and not plan.calls


def test_captured_navigation_contract_rejects_inconsistent_presentation() -> None:
    _, current = offered()
    raw = snapshot_description(current)
    raw["contract_text"] = ""
    with pytest.raises(ValueError, match="contract text differs"):
        restore_snapshot(raw)


def test_no_unique_source_keeps_url_input_and_denied_navigation_stays_denied() -> None:
    session, _ = offered()
    session.task_text = f"查看 {URL} 和 https://example.com/other。"
    session.required_source_urls = frozenset({URL, "https://example.com/other"})
    multiple = offer_browser_call(build_turn_snapshot(session), session, "browser_navigate")
    assert multiple.task_navigation_url is None
    assert next(spec for spec in multiple.model_specs if spec.name == "browser_navigate").required == ("url",)
    session.controller_denied = ("browser_navigate",)
    denied = offer_browser_call(build_turn_snapshot(session), session, "browser_navigate")
    assert "browser_navigate" not in denied.inventory_names
    assert "browser_navigate" not in {entry.name for entry in snapshot_catalog(denied).entries}


def wire_navigation_schema(payload: JsonObject, native: bool, tool_name: str = "browser_navigate") -> JsonObject:
    if native:
        tools = payload["tools"]
        assert isinstance(tools, list)
        for raw in tools:
            function = decode_json_object(decode_json_object(raw)["function"])
            if function["name"] == tool_name:
                return decode_json_object(function["parameters"])
    else:
        root = decode_json_object(decode_json_object(decode_json_object(payload["response_format"])["json_schema"])["schema"])
        variants = root["oneOf"]
        assert isinstance(variants, list)
        for raw in variants:
            clauses = decode_json_object(raw)["allOf"]
            assert isinstance(clauses, list)
            properties: JsonObject = {key: value for clause in clauses
                for key, value in decode_json_object(decode_json_object(clause).get("properties", {})).items()}
            if decode_json_object(properties["tool"]).get("enum") == [tool_name]:
                return decode_json_object(properties["args"])
    raise AssertionError(f"actual request has no {tool_name} contract")


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("native", [False, True])
def test_wire_contract_and_real_navigation_never_dispatch_the_old_typo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    directory = tmp_path / "run"
    if native:
        monkeypatch.setattr(gate, "ExperimentProvider", NativeExperimentProvider)
    def replies() -> Iterator[JsonObject]:
        schema = wire_navigation_schema(requests(directory)[-1], native)
        assert schema["required"] == ["target"]
        assert set(decode_json_object(schema["properties"])) == {"target"}
        yield {"tool": "browser_navigate", "args": {"url": WRONG}}
        feedback = canonical_json(requests(directory)[-1]["messages"])
        assert 'target' in feedback
        yield {"tool": "browser_navigate", "args": {"target": "task"}}
        yield {"tool": "browser_read", "args": {}}
        yield {"tool": "done", "args": {"summary": f"已打开并读取用户指定页面。来源：{URL}"}}
    install_http(monkeypatch, replies(), native=native)
    result = decode_json_object(gate.run_case(directory, "chat", Target("http://scripted.test/v1", "fixture", 123456789, 10),
        declare_required_tools=False, max_turns=5,
        task_transform=lambda _task: f"请调用 browser_navigate 打开 {URL}，读取页面后说明打开结果。",
        additional_required_tools=(require_tool_success("browser_navigate"),)))
    assert result["stop_reason"] == "done", result
    navigations = result["navigations"]
    assert isinstance(navigations, list) and WRONG not in navigations
    navigated = [row.result for row in tool_receipts(directory / "state") if row.result.call.name == "browser_navigate"]
    assert navigated and all(row.call.args == {"url": URL} and row.ok for row in navigated)
