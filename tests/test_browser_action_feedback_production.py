"""Browser effects, observation failures and pending actions at real request boundaries."""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Error, Locator, Page

from codey.automation.browser_session import BrowserSession, BrowserToolOutput
from codey.types.json_value import JsonObject, parse_json_object
from tests.test_receipt_bound_edit_surface_probe import install_http
from tests.test_structured_actions import action
from tests.test_structured_actions_production import TARGET
from tools.completion_artifact_audit import tool_receipts
from tools.structured_actions_live_gate import run


def requests(directory: Path) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines():
        row = parse_json_object(line)
        if row.get("type") == "wire_attempt" and row.get("phase") == "request":
            exchange = row["exchange"]
            assert isinstance(exchange, int)
            rows.append((exchange, json.dumps(row["payload"], ensure_ascii=False)))
    return rows


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_delivered_click_observation_failure_reaches_model_before_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = BrowserSession._action_observation
    failures = 4

    def fail_read(self: Locator, *, timeout: float | None = None) -> str:
        raise Error("controlled body observation failure")

    def delivered(page: Page, kind: str, ref: str) -> BrowserToolOutput:
        nonlocal failures
        if kind == "click" and failures:
            failures -= 1
            with monkeypatch.context() as scoped:
                scoped.setattr(Locator, "inner_text", fail_read)
                return original(page, kind, ref)
        return original(page, kind, ref)

    monkeypatch.setattr(BrowserSession, "_action_observation", staticmethod(delivered))
    click = action("click", target={"role": "button", "name": "Load record"})
    replies = [action("navigate", target="task"), action("snapshot"),
        action("fill", target={"role": "textbox", "name": "Locality"}, value="福州"),
        *([click] * 4), action("snapshot"), click,
        action("wait", condition="text", value="鱼丸", timeout_ms=5000), action("read"),
        action("eval", code="window.__contractToken"), action("tabs"),
        {"tool": "done", "args": {"summary": "已观察并完成操作。\n\n净重173克，校验码 QUARTZ_851。"}}]
    install_http(monkeypatch, replies)
    directory = tmp_path / "run"
    row = run(directory, "chat", TARGET)
    assert row["passed"] is True, row
    clicks = [item.result for item in tool_receipts(directory / "state") if item.result.call.name == "browser_click"]
    assert len(clicks) == 5 and [item.ok for item in clicks] == [False] * 4 + [True]
    for item in clicks[:4]:
        value = item.canonical["value"]
        assert isinstance(value, dict) and value["delivery"] == "delivered" and value["observation"] == "unavailable"
    wire = dict(requests(directory))
    for exchange in range(5, 9):
        assert "Observe the page before deciding another action" in wire[exchange]
    assert "Do not call it again with identical arguments" not in wire[8]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_effect_read_cycle_reminder_reaches_the_next_model_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    click = action("click", target={"role": "button", "name": "Load record"})
    replies = [action("navigate", target="task"), action("snapshot"),
        action("fill", target={"role": "textbox", "name": "Locality"}, value="福州"),
        *([click, action("read")] * 3), action("wait", condition="text", value="鱼丸", timeout_ms=5000),
        action("eval", code="window.__contractToken"), action("tabs"),
        {"tool": "done", "args": {"summary": "已加载并观察记录。\n\n净重173克，校验码 QUARTZ_851。"}}]
    install_http(monkeypatch, replies)
    directory = tmp_path / "run"
    row = run(directory, "chat", TARGET)
    assert row["passed"] is True, row
    wire = dict(requests(directory))
    assert "does not establish whether an effect occurred" in wire[10]
    assert "outstanding requirements" in wire[10]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_rejection_and_edit_conflict_feedback_use_the_request_action_interface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "run"
    explanation = "已填写并加载当前记录。\n\n净重173克，校验码 QUARTZ_851。"

    def replies() -> Iterator[JsonObject]:
        yield action("navigate", target="task")
        yield action("snapshot")
        yield action("fill", target={"role": "textbox", "name": "Locality"}, value="福州")
        yield action("click", target={"role": "button", "name": "Load record"})
        yield action("read")
        yield {"tool": "done", "args": {"summary": explanation}}
        yield action("wait", condition="text", value="鱼丸", timeout_ms=5000)
        yield action("eval", code="window.__contractToken")
        yield action("tabs")
        edit = action("edit_json", path="result.json", changes={"amount": 173, "code": "QUARTZ_851"})
        yield edit
        yield edit
        yield {"tool": "done", "args": {"summary": explanation}}

    install_http(monkeypatch, replies())
    row = run(directory, "hybrid", TARGET)
    assert row["passed"] is True, row
    wire = dict(requests(directory))
    assert "tool_execution_missing" in wire[7]
    assert '\\"kind\\": \\"wait\\"' in wire[7] or '"kind": "wait"' in wire[7]
    assert "window.__contractToken" in wire[7]
    assert "call tools.browser_eval" not in wire[7]
    assert "call tools.browser_wait" not in wire[7]
    assert "Resubmit the edit_json action" in wire[11]
    assert "const read" not in wire[11]
    assert row["version_check_reached"] is True
