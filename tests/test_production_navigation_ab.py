"""Fixed replies through production schemas, kernel, Chromium and durable receipts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.types.json_value import JsonObject, decode_json_object
from tests.test_task_contract_fixed_replies import TARGET, install_original_http
from tools.completion_artifact_audit import tool_receipts
from tools.navigation_candidate_ab import Arm
from tools.production_navigation_ab import (
    READY,
    ExpectedFlow,
    Scenario,
    evaluate,
    production_cases,
    read_jsonl,
    run_arm,
)

pytestmark = pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")


def action(code: str, inputs: JsonObject | None = None) -> JsonObject:
    args: JsonObject = {"code": code}
    if inputs is not None:
        args["inputs"] = inputs
    return {"tool": "codemode", "args": args}


def fixed_flow(scenario: Scenario, expected: ExpectedFlow, arm: Arm, index: int, *, mode: str = "click") -> list[JsonObject]:
    search = action("return unwrap(await tools.web_search({query:inputs.query}));", {"query": scenario.source.question})
    rid = ("r3", "r1", "r2", "r2")[index]
    opened = (action("return unwrap(await tools.open_url({url:inputs.url}));", {"url": expected.entry}) if arm == "A" else
              action("return unwrap(await tools.open_result({result_id:inputs.rid}));", {"rid": rid}))
    actions = [search, opened]
    if scenario.kind != "search":
        code = "const s=unwrap(await tools.browser_snapshot({}));const ref=s.elements.find(x=>x.name===inputs.name).ref;"
        if mode == "navigate":
            fallback = next(url for url, _ in scenario.source.documents if "/available-stock?" in url)
            code += "unwrap(await tools.browser_navigate({url:inputs.fallback}));"
        else:
            fallback = ""
            if mode == "stale":
                code += "unwrap(await tools.browser_navigate({url:inputs.entry}));"
            code += "unwrap(await tools.browser_click({ref}));"
            if mode != "no_wait":
                code += "unwrap(await tools.browser_wait({condition:'text',value:inputs.ready,timeout_ms:5000}));"
        code += "return unwrap(await tools.browser_read({}));"
        actions.append(action(code, {"name": expected.click_name, "entry": expected.entry, "fallback": fallback, "ready": READY}))
    done: JsonObject = {"tool": "done", "args": {"summary": "；".join(expected.facts)}}
    return [*actions, done, done, done, done]


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("arm", ["A", "B"])
def test_native_source_interfaces_and_real_click_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, index: int, arm: Arm,
) -> None:
    scenario, expected = production_cases()[index]
    actions = fixed_flow(scenario, expected, arm, index)
    install_original_http(monkeypatch, [json.dumps(item, ensure_ascii=False) for item in actions])
    directory = tmp_path / "run"
    result = run_arm(directory, scenario, expected, arm, TARGET, max_turns=6)
    assert result["passed"] is True, result
    rows = tool_receipts(directory / "state")
    assert any(row.result.call.name == ("open_url" if arm == "A" else "open_result") and row.result.ok for row in rows)
    sources = decode_json_object(result["sources"])
    assert sources["entry_from_observed_candidate"] is True
    if scenario.kind != "search":
        clicks = decode_json_object(result["clicks"])
        assert clicks["trusted_target_click_refs"] and clicks["snapshot_click_wait_read_chains"]
        actual = [row.result for row in rows if row.result.call.name == "browser_click"]
        assert len(actual) == 1 and actual[0].ok
        observed = decode_json_object(actual[0].canonical["value"])
        assert observed["action"] == "click" and observed["delivery"] == "delivered"
        # The production receipt correctly does not claim application success.
        assert observed["effect_verified"] is False
        snapshots = [row.result for row in rows if row.result.call.name == "browser_snapshot"]
        initial_text = str(decode_json_object(snapshots[0].canonical["value"])["text"])
        assert READY not in initial_text
        assert not any(fact in initial_text for fact in expected.facts)
        assert not any(row.result.call.name == "browser_navigate" for row in rows)
    if scenario.kind == "event_click":
        # preventDefault kept the current URL; the handler, not href navigation,
        # supplied the facts after a real wait and a committed full read.
        assert expected.entry == expected.final_url
        assert any(row.result.call.name == "browser_wait" and row.result.ok for row in rows)
        observations = read_jsonl(directory / "click-observations.jsonl")
        before = decode_json_object(observations[0]["before"])
        element = decode_json_object(before["element"])
        assert element["href"] != expected.final_url
    # Both paths use native references; there is no experimental candidate schema.
    wire = (directory / "provider.jsonl").read_text(encoding="utf-8")
    assert "candidate_id" not in wire
    assert "Search result references" in wire
    assert sources["expected_interface"] == ("open_url" if arm == "A" else "open_result")


@pytest.mark.parametrize("arm", ["A", "B"])
def test_href_navigation_does_not_substitute_for_event_click(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: Arm,
) -> None:
    scenario, expected = production_cases()[3]
    actions = fixed_flow(scenario, expected, arm, 3, mode="navigate")
    install_original_http(monkeypatch, [json.dumps(item, ensure_ascii=False) for item in actions])
    directory = tmp_path / "run"
    result = run_arm(directory, scenario, expected, arm, TARGET, max_turns=5)
    assert result["passed"] is False
    assert decode_json_object(result["sources"])["entry_from_observed_candidate"] is True
    assert decode_json_object(result["clicks"])["trusted_target_click_refs"] == []
    assert result["complete_fact_read_refs"] == []
    rows = tool_receipts(directory / "state")
    assert any(row.result.call.name == "browser_navigate" and row.result.ok for row in rows)
    assert not any(row.result.call.name == "browser_click" for row in rows)
    assert any(row.result.call.name == "browser_read" and "静态降级页" in row.result.model_text for row in rows)


@pytest.mark.parametrize("mode", ["stale", "no_wait"])
def test_stale_ref_and_missing_wait_are_not_a_complete_production_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    scenario, expected = production_cases()[3]
    actions = fixed_flow(scenario, expected, "B", 3, mode=mode)
    install_original_http(monkeypatch, [json.dumps(item, ensure_ascii=False) for item in actions])
    directory = tmp_path / "run"
    result = run_arm(directory, scenario, expected, "B", TARGET, max_turns=5)
    assert result["passed"] is False
    assert decode_json_object(result["clicks"])["click_chain_complete"] is False
    rows = tool_receipts(directory / "state")
    clicks = [row.result for row in rows if row.result.call.name == "browser_click"]
    assert len(clicks) == 1
    if mode == "stale":
        assert not clicks[0].ok
        assert decode_json_object(clicks[0].canonical["value"])["reason"] == "stale_ref"
    else:
        assert clicks[0].ok
        assert not any(row.result.call.name == "browser_wait" for row in rows)


def test_fact_delivery_and_trusted_click_witness_are_independently_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario, expected = production_cases()[3]
    actions = fixed_flow(scenario, expected, "B", 3)
    install_original_http(monkeypatch, [json.dumps(item, ensure_ascii=False) for item in actions])
    directory = tmp_path / "run"
    assert run_arm(directory, scenario, expected, "B", TARGET)["passed"] is True
    rows = tool_receipts(directory / "state")
    for answer in (expected.facts[0], "37 件，WEST_0037，NORTH_0112"):
        assert evaluate(directory, rows, expected, "B", scenario.kind, answer, "done")["passed"] is False
    trace = read_jsonl(directory / "click-observations.jsonl")
    trace[0]["after"] = {"events": [{"id": expected.click_id, "trusted": False}]}
    (directory / "click-observations.jsonl").write_text(json.dumps(trace[0]) + "\n", encoding="utf-8")
    assert evaluate(directory, rows, expected, "B", scenario.kind, "；".join(expected.facts), "done")["passed"] is False


def test_no_user_url_and_no_click_binding_ab_is_invented() -> None:
    cases = production_cases()
    assert len(cases) == 4
    for scenario, expected in cases:
        assert "http" not in scenario.source.question
        assert not any(fact in scenario.source.question for fact in expected.facts)
        assert len(scenario.source.search_html.split('class="b_algo"')) >= 4
    event, expected = cases[3]
    listing = dict(event.source.documents)[expected.entry]
    assert "event.preventDefault()" in listing and "setTimeout" in listing
    assert not all(fact in listing for fact in expected.facts)  # text is delivered only by handler execution


@pytest.mark.parametrize("arm", ["A", "B"])
def test_invalid_source_locator_preserves_search_and_cannot_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: Arm,
) -> None:
    scenario, expected = production_cases()[0]
    actions = fixed_flow(scenario, expected, arm, 0)
    actions[1] = (action("return unwrap(await tools.open_url({url:inputs.url}));",
        {"url": expected.entry.replace("autumn-2026", "autumn_2026")}) if arm == "A" else
        action("return unwrap(await tools.open_result({result_id:'r999'}));"))
    install_original_http(monkeypatch, [json.dumps(item, ensure_ascii=False) for item in actions])
    directory = tmp_path / "run"
    result = run_arm(directory, scenario, expected, arm, TARGET, max_turns=5)
    assert result["passed"] is False
    assert decode_json_object(result["sources"])["entry_from_observed_candidate"] is False
    rows = tool_receipts(directory / "state")
    assert any(row.result.call.name == "web_search" and row.result.ok for row in rows)
    source_name = "open_url" if arm == "A" else "open_result"
    assert any(row.result.call.name == source_name and not row.result.ok for row in rows)
    if arm == "B":
        assert any("unknown open_result id" in row.result.model_text for row in rows)
