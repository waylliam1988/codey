"""An unsettled query protects its target while unrelated recovery remains usable."""
from __future__ import annotations

import pytest

from codey.automation.browser_session import BrowserSession
from codey.operations.browser_task_handoff import browser_task_handoff
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_protocol import build_turn_snapshot, offer_browser_tools
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object, require_json_string
from tests.test_browser_context_delivery import URL, elements, install
from tests.test_browser_context_delivery import production_browser as production_browser


def control(browser: BrowserSession, name: str) -> str:
    return next(require_json_string(row["ref"], "ref") for row in elements(browser.snapshot().observation)
                if row["name"] == name)


def test_occluded_query_is_proven_not_dispatched_and_close_remains_available(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    assert browser.navigate(URL).ok
    install(browser, '''<button onclick="window.queries=(window.queries||0)+1">Query</button>
        <div style="position:fixed;inset:0;background:white;z-index:100">
        <button onclick="this.parentElement.remove()">Close</button></div>''')
    output = browser.click(control(browser, "Query"), query=True)
    assert not output.ok
    assert output.observation["delivery"] == "not_delivered"
    assert output.observation["reason"] == "occluded"
    assert "query_baseline" not in output.observation
    assert delegate.execute(ToolCall("browser_click", {"ref": control(browser, "Close")}))[1]
    assert browser.click(control(browser, "Query")).ok
    assert browser.evaluate("window.queries").observation["value"] == 1


def test_nested_shadow_target_checks_outer_overlay_before_dispatch(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    _, _, browser = production_browser
    assert browser.navigate(URL).ok
    install(browser, '<div id="host"></div>')

    assert browser.evaluate('() => {\n            const outer = document.querySelector(\'#host\').attachShadow({mode:\'open\'});\n            outer.innerHTML=\'<div id="inner"></div>\';\n            const inner = outer.querySelector(\'#inner\').attachShadow({mode:\'open\'});\n            inner.innerHTML=\'<button onclick="window.queries=(window.queries||0)+1">Query</button>\';\n            document.body.insertAdjacentHTML(\'beforeend\',\n                \'<div style="position:fixed;inset:0;background:white;z-index:100">\' +\n                \'<button onclick="this.parentElement.remove()">Close</button></div>\');\n        }').ok
    output = browser.click(control(browser, "Query"))
    assert not output.ok and output.observation["reason"] == "occluded"
    assert output.observation["delivery"] == "not_delivered"
    assert browser.click(control(browser, "Close")).ok
    assert browser.click(control(browser, "Query")).ok
    assert browser.evaluate("window.queries").observation["value"] == 1


@pytest.mark.parametrize("delivery", ["delivered", "uncertain"])
def test_unsettled_original_is_not_replayed_but_different_recovery_target_works(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], delivery: str,
) -> None:
    session, tools, browser = production_browser
    delegate = ExecutionDelegate(session=session, research_tools=tools)
    assert browser.navigate(URL).ok
    install(browser, '''<button onclick="window.queries=(window.queries||0)+1">Query</button>
        <button onclick="window.closed=true">Close</button><article>No query result yet.</article>''')
    ref = control(browser, "Query")
    output = browser.click(ref, query=True)
    assert output.ok
    baseline = decode_json_object(output.observation["query_baseline"])
    audit: JsonObject = {"query_baseline": baseline}
    session._memory_results["query"] = ToolResult(ToolCall("browser_click", {"ref": ref}), output.model_text,
        ok=delivery == "delivered", canonical={"value": {"delivery": delivery}}, audit=audit)
    project_execution_record(session, "query", session._memory_results["query"].call,
                             session._memory_results["query"], ok=delivery == "delivered")
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    offered, _ = browser_task_handoff(session, snapshot, CompletionContext(browser_document=browser.document_identity()))
    assert "browser_click" in offered.tool_names  # Recovery targets remain discoverable; dispatch guards the pending target.
    replay, ok, _ = delegate.execute(ToolCall("browser_click", {"ref": control(browser, "Query")}))
    assert not ok and decode_json_object(replay.canonical["value"])["delivery"] == "not_delivered"
    assert delegate.execute(ToolCall("browser_click", {"ref": control(browser, "Close")}))[1]
    assert browser.evaluate("window.queries").observation["value"] == 1
