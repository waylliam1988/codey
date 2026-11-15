"""Declared Research workflows reuse browser programs without losing evidence scope."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.operations.kernel_protocol import build_turn_snapshot, offer_browser_call
from codey.operations.task_session import TaskSession
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

        snapshot = replace(snapshot, research_scope=None)
    offered = offer_browser_call(snapshot, session, "browser_wait", research=True)
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
        yield {"tool": "browser_navigate", "args": {"url": gate.URL}}
        yield {"tool": "browser_snapshot", "args": {}}
        yield {"tool": "browser_fill", "args": {"ref": reference("Locality"), "value": "福州"}}
        click: JsonObject = {"ref": reference("Load record")}
        if composed:
            click["purpose"] = "result"
        yield {"tool": "browser_click", "args": click}
        if not composed:
            yield {"tool": "browser_wait", "args": {"condition": "text", "value": "鱼丸", "timeout_ms": 5000}}
            yield {"tool": "browser_read", "args": {}}
        label = assert_submitting(directory)
        yield fixture.submission(label, current=True)

    install_http(monkeypatch, replies(), native=native)
    result = gate.run_case(directory, "research", Target("http://scripted.test/v1", "fixture", 123456789, 10),
                           strict_research=True, research_sequence=sequence, restore_snapshots=True, max_turns=7)
    assert result["passed"] is True, result
    calls = [row.result for row in tool_receipts(directory / "state")]
    reads = [result for result in calls if result.call.name == "browser_read" and result.ok]
    assert len(reads) == 1 and "research_observation" in reads[0].canonical and "read_passages" in reads[0].canonical
    assert sum(result.call.name == "browser_wait" and result.ok for result in calls) == 1
    assert sum(result.call.name == "browser_click" and result.ok for result in calls) == 1
    assert any(result.ok and result.call.name == "submit_research_answer" for result in calls)
