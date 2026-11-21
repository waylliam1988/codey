"""The experiment changes presentation, never the production execution contract."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from playwright.sync_api import Route

from codey.operations.browser_references import BrowserReferences
from codey.operations.declared_field_input import model_spec
from codey.operations.kernel_protocol import ModelInputBinding, build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.research.ledger import OpenedSource, ResearchLedger
from codey.research.ledger_receipts import source_snapshot
from codey.research.passages import describe_window, restore_passages
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, JsonValue, canonical_json_object, decode_json_object, parse_json_object
from tests.test_research_submission_handoff_production import install_http, requests
from tools import browser_choice_presentation_ab as experiment
from tools.browser_experiment_sources import ResumableBrowserSearchProvider as BrowserSearchProvider
from tools.browser_production_live_gate import URL
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Arm, ExperimentProvider, Target

TARGET = Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10)


def observed_session(*, fill: bool = True) -> TaskSession:
    grants = {"control", "web.read", "browser.observe"}
    if fill:
        grants.add("browser.interact")
    return TaskSession(TaskPolicy(frozenset(grants)), task_kind="chat", task_text="Use codemode only.")


def observation() -> BrowserReferences:
    controls: list[JsonValue] = [
        {"ref": "field", "role": "textbox", "name": "Harbor", "context_id": "current", "disabled": False},
        {"ref": "current", "role": "button", "name": "Search", "context_id": "current", "disabled": False},
        {"ref": "archive", "role": "button", "name": "Search", "context_id": "archive", "disabled": False},
    ]
    return BrowserReferences.observe("snapshot", {"complete": True, "elements_truncated": False,
        "generation": 1, "elements": controls, "contexts": [
            {"id": "current", "label": "Current"}, {"id": "archive", "label": "Archive"}]},
        {"target_id": "tab", "loader_id": "doc", "url": URL})


def test_cards_keep_all_contexts_without_selecting_a_business_target() -> None:
    session = observed_session()
    snapshot = replace(build_turn_snapshot(session), browser_references=observation(), browser_followups=True)
    before = snapshot
    text = experiment.cards(session, snapshot)
    assert "Current" in text and "Archive" in text
    assert "supply value" in text and "browser_fill(inputs)" in text
    assert "Use the existing one-step call" in text
    assert snapshot is before
    assert "福州" not in text and "NB-41" not in text and "QUARTZ_851" not in text
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        row = parse_json_object(line)
        if row.get("call") is not None:
            action = row["call"]
            assert not normalize_turn(decode_text_turn(json.dumps(action)), snapshot=snapshot).protocol_error


def test_cards_do_not_offer_denied_interaction() -> None:
    session = observed_session(fill=False)
    snapshot = replace(build_turn_snapshot(session), browser_references=observation())
    text = experiment.cards(session, snapshot)
    assert '"operation": "browser_fill"' not in text
    assert '"operation": "browser_click"' not in text


def test_bound_copy_uses_existing_path_input_without_manufacturing_content() -> None:
    session = observed_session()
    snapshot = build_turn_snapshot(session)
    original = next(spec for spec in snapshot.frozen_specs if spec.name == "codemode")
    bound = ModelInputBinding("declared_fields", model_spec(original, ("destination.json",)), ("read-receipt",))
    snapshot = replace(snapshot, input_binding=bound)
    text = experiment.cards(session, snapshot)
    assert "destination.json" in text and "already bound" in text
    assert "read-receipt" not in text and "browser_fill" not in text


@pytest.mark.parametrize("arm", ["A", "B"])
def test_provider_only_appends_a_presentation_message(tmp_path: Path, arm: Arm) -> None:
    provider = experiment.ChoiceProvider(TARGET, tmp_path, arm, ())
    provider.presentation = "Current legal choices"
    payload: dict[str, object] = {"messages": [{"role": "user", "content": "Original task"}],
                                  "response_format": {"type": "json_schema", "schema": {"tool": "codemode"}}}
    before = canonical_json_object(payload)
    try:
        provider.configure_experiment(payload)
        assert payload["seed"] == TARGET.seed
        actual = canonical_json_object(payload)
        assert actual["response_format"] == before["response_format"]
        messages = actual["messages"]
        assert isinstance(messages, list)
        if arm == "A":
            assert messages == before["messages"]
        else:
            assert messages[:-1] == before["messages"]
            assert messages[-1] == {"role": "user", "content": provider.presentation}
        assert experiment.ChoiceProvider.normalize_reply is ExperimentProvider.normalize_reply
    finally:
        provider.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("name", experiment.CASES)
def test_fixture_is_solvable_and_passive_audit_preserves_refs(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    case = experiment.fixture(name, 0)
    browser = BrowserSearchProvider(headless=True)

    def route(request: Route) -> None:
        request.fulfill(status=200, content_type="text/html; charset=utf-8", body=case.body)

    monkeypatch.setattr(browser, "_guard_request", route)
    session = browser.browser
    try:
        # Match the live runner's initial open_url acquisition, including its
        # readable-content checks; navigate alone bypasses that source contract.
        fetched = browser.fetch(URL)
        assert fetched.get("status") == "ok", fetched
        observation = session.snapshot()
        assert observation.ok
        def choose(role: str, name: str = "", selector: str = "") -> str:
            from codey.automation.browser_contracts import BrowserAction

            output = session.action(BrowserAction.LOCATE, {"role": role,
                **({"name": name} if name else {}), **({"selector": selector} if selector else {})})
            rows = output.observation["candidates"]
            assert output.ok and isinstance(rows, list) and len(rows) == 1, output
            value = decode_json_object(rows[0])["ref"]
            assert isinstance(value, str)
            return value

        if name == "pagination":
            assert session.click(choose("button", "下一页")).ok
            assert session.click(choose("button", "打开 ZX-62")).ok
        elif name != "visible":
            scope = 'section[aria-label="当前库存"] ' if name == "duplicate" else ""
            assert session.fill(choose("textbox", selector=scope + "input"), "NB-41" if name == "duplicate" else "福州").ok
            assert session.click(choose("button", selector=scope + "button")).ok
        assert session.wait("text", case.code, 5000).ok
        generation = session.generation
        counts = decode_json_object(session.inspect_state("window.counts"))
        assert counts == {"clicks": case.clicks, "fills": case.fills, "wrong": 0}
        assert session.generation == generation

    finally:
        browser.close()


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("arm", ["A", "B"])
def test_live_harness_uses_production_receipts_and_preserves_actual_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: Arm,
) -> None:
    directory = tmp_path / "run"
    case = experiment.fixture("visible", 0)

    def replies() -> Iterator[JsonObject]:
        yield {"tool": "codemode", "args": {"code": "return unwrap(await tools.browser_read({}));", "inputs": {}}}
        ledger = ResearchLedger()
        for row in tool_receipts(directory / "state"):
            if row.result.ok and row.result.call.name in {"browser_read", "open_url"}:
                restore_passages(ledger, row.result.canonical.get("research_observation"),
                                 row.result.canonical.get("read_passages"))
        reference = next(iter(ledger.read_passages))
        yield {"tool": "done", "args": {"summary": f"净重 {case.amount} 克，校验码 {case.code} [{reference}]"}}

    install_http(monkeypatch, replies(), native=False)
    result = experiment.run_arm(directory, case, arm, TARGET)
    assert result["passed"] is True, result
    assert result["page_state"] == {"clicks": 0, "fills": 0, "wrong": 0}
    wire = requests(directory)
    assert all((experiment.CARD_HEADER in json.dumps(row["messages"])) is (arm == "B") for row in wire)
    assert all("response_format" in row for row in wire)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_failed_source_does_not_turn_passive_audit_into_controller_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = replace(experiment.fixture("visible", 0), body="<html><body><p>Short</p></body></html>")
    install_http(monkeypatch, iter([{"tool": "done", "args": {"summary": "来源未能读取，无法核实。"}}]), native=False)
    result = experiment.run_arm(tmp_path / "run", case, "B", TARGET)
    assert result["model_calls"] == 1, result
    assert result["stop_reason"] != "controller_failure", result
    assert result["passed"] is False
    assert result["page_state"] == {}


def source_result(text: str) -> ToolResult:
    source = OpenedSource(URL, URL, "Records", "2026-11-19T00:00:00+00:00",
                          hashlib.sha256(text.encode()).hexdigest()[:16])
    canonical = canonical_json_object({"source_id": "s1", "research_observation": source_snapshot(source, text, {}),
        "read_passages": describe_window(URL, source.text_hash, text, 0, len(text))})
    return ToolResult(ToolCall("browser_read", {}), text, ok=True, canonical=canonical)


@pytest.mark.parametrize("citation", ["source", "rendered", "passage", "snapshot", "click"])
def test_evidence_audit_accepts_actual_read_and_browser_observation_forms(citation: str) -> None:
    text = "实际净重 173 克，校验码 QUARTZ_851。"
    read = source_result(text)
    results = [read]
    answer = "净重 173 克，QUARTZ_851 [s1]"
    if citation == "rendered":
        answer = f"净重 173 克，QUARTZ_851 [s1](<{URL}>)"
    elif citation == "passage":
        ledger = ResearchLedger()
        restore_passages(ledger, read.canonical["research_observation"], read.canonical["read_passages"])
        answer = f"净重 173 克，QUARTZ_851 [{next(iter(ledger.read_passages))}]"
    elif citation in {"snapshot", "click"}:
        results = [source_result("尚未加载记录。"),
            ToolResult(ToolCall("browser_" + citation, {}), text, ok=True,
                       canonical={"value": {"page_url": URL, "text": text}})]
    evidence = experiment.verify_evidence(results, answer, ("173", "QUARTZ_851"))
    assert evidence["correct_evidence_cited"] is True
    assert evidence["formal_passage_evidence"] is (citation not in {"snapshot", "click"})
    assert evidence["observed_body_evidence"] is (citation in {"snapshot", "click"})


@pytest.mark.parametrize("failure", ["unread", "unknown", "wrong_url", "code", "failed", "return_only", "old_passage"])
def test_evidence_audit_rejects_unsupported_answers(failure: str) -> None:
    text = "实际净重 173 克，校验码 QUARTZ_851。"
    answer = "净重 173 克，QUARTZ_851 [s1]"
    results = [source_result(text)]
    if failure == "unread":
        results = [source_result("尚未加载记录。")]
    elif failure == "unknown":
        answer = "净重 173 克，QUARTZ_851 [s999]"
    elif failure == "wrong_url":
        answer = "净重 173 克，QUARTZ_851 [s1](<https://other.test/>)"
    elif failure == "code":
        answer = "净重 173 克，QUARTZ_851\n\n```\n[s1]\n```"
    elif failure in {"failed", "return_only"}:
        results = [source_result("尚未加载记录。"),
            ToolResult(ToolCall("browser_snapshot" if failure == "failed" else "codemode", {}), text,
                       ok=failure != "failed", canonical={"value": {"page_url": URL, "text": text}})]
    else:
        old = source_result("尚未加载记录。")
        ledger = ResearchLedger()
        restore_passages(ledger, old.canonical["research_observation"], old.canonical["read_passages"])
        answer = f"净重 173 克，QUARTZ_851 [{next(iter(ledger.read_passages))}]"
        results = [old, source_result(text)]
    assert experiment.verify_evidence(results, answer, ("173", "QUARTZ_851"))["correct_evidence_cited"] is False
