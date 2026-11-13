"""Check the final browser experiment with fixed replies and real browser receipts."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.automation.browser_session import BrowserSession
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.types.json_value import JsonObject, decode_json_object
from tests.test_receipt_bound_edit_surface_probe import install_http
from tools import browser_chat_followup_gate as gate
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target

TARGET = Target("http://scripted.test/v1", "fixture", 123456789, 15)


def request(tool: str, **args: object) -> JsonObject:
    return {"tool": tool, "args": decode_json_object(args)}


def observed(directory: Path, name: str) -> str:
    snapshots = [item.result for item in tool_receipts(directory / "state") if item.result.ok
                 and item.result.call.name == "browser_snapshot"]
    elements = decode_json_object(snapshots[-1].canonical["value"])["elements"]
    assert isinstance(elements, list)
    return next(str(e["ref"]) for e in elements if isinstance(e, dict) and e.get("name") == name)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_paired_matrix_checks_real_provider_schema_and_costs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = []
    for arm in gate.ARMS:
        directory = tmp_path / arm

        def proposals(arm: str = arm, directory: Path = directory) -> Iterator[JsonObject]:
            snapshot = request("browser_snapshot") if arm != "codemode" else request(
                "codemode", code="return unwrap(await tools.browser_snapshot({}));")
            yield snapshot
            fill = {"ref": observed(directory, "备注"), "value": gate.base.NOTE}
            yield request("browser_fill", **fill) if arm != "codemode" else request(
                "codemode", code="return unwrap(await tools.browser_fill(inputs));", inputs=fill)
            if arm in {"codemode", "direct"}:
                yield snapshot
            click = {"ref": observed(directory, "查询记录")}
            if arm == "codemode":
                yield request("codemode", code="return unwrap(await tools.browser_click(inputs));", inputs=click)
            elif arm == "purpose":
                yield request("browser_click", **click, purpose="result")
            else:
                yield request("browser_click", **click)
            if arm != "purpose":
                yield request("browser_read") if arm != "codemode" else request(
                    "codemode", code="return unwrap(await tools.browser_read({}));")
            yield request("done", summary="备注已受理，回执码 NOTE_624。[s1]")

        install_http(monkeypatch, proposals())
        row = gate.run_unit(directory, "quoted", gate.CASES["quoted"], arm, TARGET)
        assert row["passed"], row
        assert row["actual_clicks"] == 1
        assert row["query_linked_reads"] == int(arm == "purpose")
        runs.append({**row, "wording": "original", "repeat": 1})
    assert [row["model_calls"] for row in runs] == [6, 6, 5, 4]
    kwargs = {"target": TARGET, "arms": list(gate.ARMS), "cases": ["quoted"], "wordings": ["original"],
              "repeats": 1, "source": gate.production_source_digest(), "harness": gate.harness_digest()}
    assert gate.audit_matrix(tmp_path, runs, **kwargs)["http_requests"] == 21
    purpose = runs[-1]
    bad_clicks = {**purpose, "page_state": {**decode_json_object(purpose["page_state"]), "clicks": 2}}
    assert gate.score(tmp_path / "purpose", "quoted", gate.CASES["quoted"], bad_clicks)["passed"] is False
    wrong_answer = {**purpose, "answer": "已经受理，回执码 OTHER_627。[s1]"}
    assert gate.score(tmp_path / "purpose", "quoted", gate.CASES["quoted"], wrong_answer)["passed"] is False
    wrong_citation = {**purpose, "answer": "备注已受理，回执码 NOTE_624。[s9]"}
    assert gate.score(tmp_path / "purpose", "quoted", gate.CASES["quoted"], wrong_citation)["passed"] is False
    with pytest.raises(AssertionError, match="duplicate"):
        gate.audit_matrix(tmp_path, [*runs, runs[0]], **kwargs)
    with pytest.raises(AssertionError, match="different task, fixture, or authority"):
        gate.audit_matrix(tmp_path, [*runs[:-1], {**runs[-1], "fixture_digest": "changed"}], **kwargs)


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
@pytest.mark.parametrize("name", ["async", "explicit_wait"])
def test_old_page_content_never_becomes_query_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    directory = tmp_path / name

    def proposals() -> Iterator[JsonObject]:
        yield request("browser_snapshot")
        yield request("browser_fill", ref=observed(directory, "城市"), value="福州")
        yield request("browser_click", ref=observed(directory, "应用"), purpose="result")
        yield request("done", summary="福州样本：净重 263 克，校验码 FRESH_263。[s1]")

    install_http(monkeypatch, proposals())
    row = gate.run_unit(directory, name, gate.CASES[name], "purpose", TARGET)
    assert row["passed"] and row["wait_order"], row
    assert row["actual_clicks"] == 1 and row["stale_body_reads"] == 0 and row["query_linked_reads"] == 1
    assert row["fill_followups"] == row["result_followups"] == 1


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_timeout_blocks_old_answer_and_recovers_by_reading_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "timeout"
    real_capture = gate.guard_capture
    captures = 0

    def capture(browser: BrowserSession, baseline: JsonObject, *, wait_seconds: float) -> dict[str, object]:
        nonlocal captures
        captures += 1
        return real_capture(browser, baseline, wait_seconds=0 if captures == 1 else 6)

    monkeypatch.setattr(gate, "guard_capture", capture)

    def proposals() -> Iterator[JsonObject]:
        yield request("browser_snapshot")
        yield request("browser_fill", ref=observed(directory, "城市"), value="福州")
        yield request("browser_click", ref=observed(directory, "应用"), purpose="result")
        yield request("done", summary="北京样本 41 克，STALE_041。[s1]")
        yield request("browser_read")
        yield request("done", summary="福州样本 263 克，FRESH_263。[s1]")

    install_http(monkeypatch, proposals())
    row = gate.run_unit(directory, "async", gate.async_case(delay=3500), "purpose", TARGET)
    assert row["passed"], row
    assert row["pending_submissions"] == 1 and row["stale_body_reads"] == 0
    assert row["actual_clicks"] == 1 and row["query_linked_reads"] == 1
    failed_reads = [item.result for item in tool_receipts(directory / "state")
                    if item.result.call.name == "browser_read" and not item.result.ok]
    assert len(failed_reads) == 1
    assert "research_observation" not in failed_reads[0].canonical


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection")
def test_controls_branch_supplies_refs_for_the_next_click(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "controls"

    def proposals() -> Iterator[JsonObject]:
        yield request("browser_snapshot")
        yield request("browser_click", ref=observed(directory, "筛选菜单"), purpose="controls")
        yield request("browser_click", ref=observed(directory, "展开 C-7"), purpose="result")
        yield request("done", summary="负责人周青，回执码 DETAIL_713。[s1]")

    install_http(monkeypatch, proposals())
    row = gate.run_unit(directory, "controls", gate.CASES["controls"], "purpose", TARGET)
    assert row["passed"] and row["actual_clicks"] == 2, row
    assert row["controls_followups"] == row["result_followups"] == row["query_linked_reads"] == 1
    assert decode_json_object(row["browser_successes"])["browser_read"] == 1


def test_model_purpose_is_stripped_and_native_id_preserved(tmp_path: Path) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"})),
                          task_kind="chat")
    with gate.intervention(tmp_path, "purpose"):
        snapshot = gate.base.expose_leaves(build_turn_snapshot(session, native=True), session)
        for purpose in gate.PURPOSES:
            reply = AssistantTurn(tool_calls=(ProviderToolCall("click-1", "browser_click", {"ref": "r", "purpose": purpose}),))
            plan = gate.normalize_candidate(reply, snapshot, "purpose")
            assert not plan.protocol_error and plan.calls[0].call_id == "click-1"
            args = plan.calls[0].args
            assert (args if purpose == "action" else decode_json_object(args["inputs"])["click"]) == {"ref": "r"}
        invalid = AssistantTurn(tool_calls=(ProviderToolCall("bad", "browser_click", {"ref": "r", "purpose": "unknown"}),))
        assert gate.normalize_candidate(invalid, snapshot, "purpose").protocol_error


def test_changed_region_requires_matching_document_and_a_settled_result() -> None:
    before: JsonObject = {"document": "a", "regions": [{"key": "record", "text": "old", "busy": False}]}
    assert gate.changed_region(before, before) == ""
    assert gate.changed_region(before, {**before, "regions": [{"key": "record", "text": "new", "busy": True}]}) == ""
    assert gate.changed_region(before, {"document": "b", "regions": [{"key": "record", "text": "new", "busy": False}]}) == ""
    assert gate.changed_region(before, {**before, "regions": [{"key": "record", "text": "new", "busy": False}]}) == "new"
    regions = before["regions"]
    assert isinstance(regions, list)
    unrelated = [*regions, {"key": "clock", "text": "tick", "busy": False}]
    assert gate.changed_region(before, {**before, "regions": unrelated}) == ""
