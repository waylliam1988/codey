"""Live operations and strict research select explicit production contracts."""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest

from codey.automation.browser_contracts import RouteDecision
from codey.types.json_value import JsonObject, parse_json_object
from tests.test_research_submission_handoff_production import install_http
from tools import unified_real_browser_probe as probe
from tools.browser_experiment_sources import ResumableBrowserSearchProvider
from tools.completion_explanation_ab import Target

URL = "https://example.com/real-probe-regression"


class FixtureSource(ResumableBrowserSearchProvider):
    def _guard_request(self, route: RouteDecision) -> None:
        route.fulfill(content_type="text/html", body='''<article id=result>Process not started.</article>
            <button onclick="document.querySelector('#result').textContent='Process confirmed COMPLETE_39';
            this.disabled=true">Advance</button>''')


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("intent", ["chat", "research"])
def test_probe_preserves_the_selected_contract_and_original_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, intent: Literal["chat", "research"],
) -> None:
    def replies() -> Iterator[JsonObject]:
        yield {"tool": "browser_snapshot", "args": {}}
        yield {"tool": "browser_click", "args": {"ref": {"role": "button", "name": "Advance"}, "purpose": "controls"}}
        yield {"tool": "browser_read", "args": {}}
        if intent == "research":
            yield {"tool": "submit_research_answer", "args": {
                "markdown": "已实际完成流程，页面显示 Process confirmed COMPLETE_39。[e2]", "state": "answered"}}
        else:
            yield {"tool": "done", "args": {"summary": f"已实际完成流程，页面显示 COMPLETE_39。来源：{URL}"}}

    install_http(monkeypatch, replies(), native=False)
    monkeypatch.setattr(probe, "BrowserSearchProvider", FixtureSource)
    directory = tmp_path / intent
    summary = probe.run_live(directory, f"请打开 {URL}，点击 Advance，读取实际结果并报告，注明来源。",
        Target("http://scripted.test/v1", "fixture", 123456789, 10), intent=intent, max_turns=4)
    assert summary.get("stop_reason") == "done", summary
    request = parse_json_object((directory / "request.json").read_text(encoding="utf-8"))
    assert request["intent"] == intent and request["strict_research"] is (intent == "research")
    receipts = summary["receipts"]
    assert isinstance(receipts, list)
    assert sum(isinstance(row, dict) and row["tool"] == "browser_click" and row["ok"] is True for row in receipts) == 1
    assert any(isinstance(row, dict) and row["tool"] == "browser_read" and row["ok"] is True
               and "COMPLETE_39" in str(row["text"]) for row in receipts)
    assert any(isinstance(row, dict) and row["tool"] == "submit_research_answer" for row in receipts) is (intent == "research")
    assert parse_json_object((directory / "summary.json").read_text(encoding="utf-8")) == summary
    with pytest.raises(FileExistsError):
        probe.run_live(directory, "Must not overwrite the first run.", Target("http://scripted.test/v1", "fixture", 0, 10))
