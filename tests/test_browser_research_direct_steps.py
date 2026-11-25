"""Declared Research workflows reuse browser programs without losing evidence scope."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.operations.browser.turn_surface import offer_browser_call
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_research_submission_handoff_production import (
    NativeExperimentProvider,
    assert_submitting,
    install_http,
)
from tools import browser_prerequisite_ab as fixture
from tools import browser_production_live_gate as gate
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target


@pytest.mark.parametrize("guard", ["ordinary", "denied", "not_exposed", "no_scope"])
def test_research_leaf_offering_cannot_expand_the_frozen_surface(guard: str) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"}),
                                    strict_research=guard != "ordinary"), task_kind="research")
    if guard == "denied":
        session.controller_denied = ("browser_wait",)
    snapshot = build_turn_snapshot(session)
    if guard == "not_exposed":
        session.model_tool_names = ("codemode",)
    if guard == "no_scope":
        from dataclasses import replace

        snapshot = replace(snapshot, research=replace(snapshot.research, scope=None))
    offered = offer_browser_call(snapshot, session, "browser_wait", research=True)
    if guard in {"ordinary", "no_scope"}:
        assert "browser_wait" in offered.tool_names and offered.policy == snapshot.policy
        assert offered.allowed == snapshot.allowed and offered.nested_tool_names == snapshot.nested_tool_names
    else:
        assert offered == snapshot


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize(("sequence", "composed"), [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("native", [False, True])
def test_research_completes_with_direct_steps_and_cites_the_post_wait_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sequence: bool, composed: bool, native: bool,
) -> None:
    directory = tmp_path / "run"
    if native:
        monkeypatch.setattr(gate, "ExperimentProvider", NativeExperimentProvider)

    def reference(name: str) -> str:
        snapshots = [row.result for row in tool_receipts(directory / "state")
                     if row.result.ok and row.result.call.name == "browser_snapshot"]
        elements = decode_json_object(snapshots[-1].canonical["value"])["elements"]
        assert isinstance(elements, list)
        return next(str(row["ref"]) for row in elements if isinstance(row, dict) and row.get("name") == name)

    def replies() -> Iterator[JsonObject]:
        yield {"tool": "browser_fill", "args": {"ref": reference("Locality"), "value": "福州"}}
        click: JsonObject = {"ref": reference("Load record")}
        if composed:
            click["purpose"] = "result"
        yield {"tool": "browser_click", "args": click}
        if not composed:
            yield {"tool": "browser_wait", "args": {"condition": "text", "value": "鱼丸", "timeout_ms": 5000}}
            yield {"tool": "browser_read", "args": {}}
        elif sequence:
            # The composed query supplies a complete result region. The explicit
            # research sequence also requires a complete page after its wait.
            yield {"tool": "browser_read", "args": {}}
        label = assert_submitting(directory, sequence=sequence)
        yield fixture.submission(label, current=True)

    install_http(monkeypatch, replies(), native=native)
    result = gate.run_case(directory, "research", Target("http://scripted.test/v1", "fixture", 123456789, 10),
                           strict_research=True, research_sequence=sequence, restore_snapshots=True, max_turns=7)
    assert result["passed"] is True, result
    calls = [row.result for row in tool_receipts(directory / "state")]
    reads = [result for result in calls if result.call.name == "browser_read" and result.ok]
    assert all("research_observation" in row.canonical and "read_passages" in row.canonical for row in reads)
    assert sum(row.call.name == "browser_navigate" and row.ok for row in calls) == 1
    assert next(index for index, row in enumerate(calls) if row.call.name == "browser_navigate") < next(
        index for index, row in enumerate(calls) if row.call.name == "browser_fill")
    assert sum(result.call.name == "browser_wait" and result.ok for result in calls) == 1
    assert sum(result.call.name == "browser_click" and result.ok for result in calls) == 1
    wait, = (index for index, row in enumerate(calls) if row.ok and row.call.name == "browser_wait")
    submission, = (index for index, row in enumerate(calls) if row.ok and row.call.name == "submit_research_answer")
    assert any(row.call.name == "browser_read" and row.ok and "QUARTZ_851" in row.model_text
               for row in calls[wait + 1:submission])
