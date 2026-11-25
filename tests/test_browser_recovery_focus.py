"""Recovery menus and source structure remain projections of original browser receipts."""
from __future__ import annotations

from dataclasses import replace

import pytest

from codey.automation.browser_session import BrowserSession
from codey.operations.browser.browser_task_handoff import bind_browser_references, browser_task_handoff
from codey.operations.browser.turn_surface import offer_browser_call, offer_browser_tools
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel.kernel_protocol import TurnSnapshot, normalize_turn
from codey.operations.kernel_facts import project_execution_record
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.answer_context import answer_context
from codey.research.answer_contract import answer_scope
from codey.research.source_document import SourceReadScope
from codey.research.source_observation import source_observation
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object
from tests.support.task_browser import install
from tests.test_browser_context_delivery import URL
from tests.test_browser_context_delivery import production_browser as production_browser
from tests.test_browser_readable_references import observed_snapshot, program
from tests.test_browser_text_change_wait import navigation_program


def reload_state(change: str, *, native: bool = False) -> tuple[TaskSession, TurnSnapshot, CompletionContext, int]:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"})),
        task_text=f"Open {URL} and complete the button sequence", required_source_urls=frozenset({URL}))
    document: JsonObject = {}
    generation = 0
    for index in range(2):
        _parent, receipts = navigation_program(index, text="Changed" if index and change == "text" else "Ready")
        for ref, result in receipts.items():
            if result.call.name == "browser_snapshot":
                value = decode_json_object(result.canonical["value"])
                raw = value["elements"]
                assert isinstance(raw, list)
                element = decode_json_object(raw[0])
                element.update(role="button", name="Begin", disabled=bool(index and change == "disabled"))
                value["elements"] = [element]
                result = replace(result, canonical={"value": value})
                document = decode_json_object(result.audit["browser_document"])
                generation = index + 1
            session._memory_results[ref] = result
            project_execution_record(session, ref, result.call, result, ok=result.ok)
        if not index and change == "action":
            action = ToolResult(ToolCall("browser_press", {"ref": "original", "key": "Enter"}), "Delivered", ok=True,
                canonical={"value": {"delivery": "delivered"}}, audit={"browser_document": document})
            session._memory_results["interaction"] = action
            project_execution_record(session, "interaction", action.call, action, ok=True)
    context = CompletionContext(browser_document=document)
    snapshot = offer_browser_call(offer_browser_tools(build_turn_snapshot(session, native=native), session), session, "browser_navigate")
    snapshot = bind_browser_references(session, snapshot, context, generation)
    return session, snapshot, context, generation


@pytest.mark.parametrize("change", ["none", "text", "disabled", "action"])
@pytest.mark.parametrize("native", [False, True])
def test_only_a_proven_unchanged_reload_focuses_the_menu_without_removing_execution_authority(change: str, native: bool) -> None:
    session, original, context, generation = reload_state(change, native=native)
    snapshot, hint = browser_task_handoff(session, original, context, generation)
    focused = change == "none"
    assert ("Recovery from the same observed page" in hint) is focused
    assert ("browser_navigate" in snapshot.tool_names) is (not focused)
    assert "browser_navigate" in snapshot.nested_tool_names
    assert set(snapshot.inventory_names) == set(original.inventory_names)
    assert dict(snapshot.custom_executors) == dict(original.custom_executors)
    assert restore_snapshot(snapshot_description(snapshot)) == snapshot
    # Recovery never replaces a chosen reload with a click or erases its original program.
    args: JsonObject = {"code": "return unwrap(await tools.browser_navigate(inputs));", "inputs": {"target": "task"}}
    reply = (AssistantTurn(tool_calls=(ProviderToolCall("reload", "codemode", args),)) if native else
        decode_text_turn('{"tool":"codemode","args":{"code":'
            '"return unwrap(await tools.browser_navigate(inputs));","inputs":{"target":"task"}}}'))
    plan = normalize_turn(reply, snapshot=snapshot)
    assert not plan.protocol_error and plan.calls[0].name == "codemode"
    assert "browser_navigate" in str(plan.calls[0].args["code"])
    assert not any(row.call.name == "browser_click" for row in session._memory_results.values())


def test_formal_browser_reads_preserve_visible_sections_tables_and_qualifications(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    assert browser.navigate(URL).ok
    install(browser, '<h2>Default checks</h2><p>Checks apply before the action.</p>'
        '<ul><li>Locator resolves to exactly one element</li><li>Element is visible</li></ul>'
        '<table><tr><th>Action</th><th>Visible</th><th>Events</th></tr>'
        '<tr><td><code>locator.click()</code></td><td>Yes</td><td>Yes</td></tr></table>'
        '<h2>Forcing actions</h2><p>The option disables non-essential checks, for example receiving events.</p>'
        '<p>南煎<strong>肝</strong><br>Second line.</p><h2 hidden>Invisible heading</h2>'
        '<details><summary>Details</summary><p>Closed secret</p></details><div id="shadow"></div>')
    assert browser.evaluate("document.querySelector('#shadow').attachShadow({mode:'open'}).innerHTML="
        "'<h3>Shadow section</h3><p>Visible shadow evidence</p>'").ok
    snapshot = observed_snapshot(session, browser)
    read = program(session, tools, snapshot, "return unwrap(await tools.browser_read({}));")
    assert read.ok, read.model_text
    formal = next(row for row in session._memory_results.values() if row.call.name == "browser_read")
    _source, text, _pages = source_observation(formal.canonical["research_observation"])
    assert "## Default checks" in text and "## Forcing actions" in text
    assert "- Locator resolves to exactly one element" in text and "- Element is visible" in text
    assert "Action\tVisible\tEvents" in text and "locator.click()\tYes\tYes" in text
    assert "non-essential checks, for example receiving events." in text
    assert "南煎肝\nSecond line." in text
    assert "### Shadow section" in text and "Visible shadow evidence" in text
    assert "Invisible heading" not in text and "Closed secret" not in text
    # The research view projects complete original list items, rather than
    # asking the model to reconstruct a checklist from the surrounding table.
    view = answer_context(session, tools.ledger, answer_scope(session))
    assert '"item_count":2' in view and '"Locator resolves to exactly one element"' in view


def test_chat_controls_also_acquire_the_post_action_formal_source(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    assert session.task_kind == "chat" and browser.navigate(URL).ok
    install(browser, '<p>Not finished</p><button onclick="document.querySelector(\'p\').textContent='
        '\'All Buttons Clicked\'">Finish</button>')
    initial = observed_snapshot(session, browser)
    clicked = program(session, tools, initial,
        "return unwrap(await tools.browser_click({ref:{role:'button',name:'Finish'},purpose:'controls'}));")
    assert clicked.ok, clicked.model_text
    reads = [row for row in session._memory_results.values() if row.call.name == "browser_read"]
    assert len(reads) == 1 and reads[0].ok
    _source, text, _pages = source_observation(reads[0].canonical["research_observation"])
    assert "All Buttons Clicked" in text
    assert session.source_ids and reads[0].canonical["read_passages"]


def test_atomic_codemode_click_can_own_its_wait_and_formal_read_without_an_implicit_read(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    assert browser.navigate(URL).ok
    install(browser, '<p>Initial state</p><button onclick="document.querySelector(\'p\').textContent='
        '\'Completed\'">Advance</button>')
    snapshot = observed_snapshot(session, browser)
    clicked = program(session, tools, snapshot,
        "return unwrap(await tools.browser_click({ref:{role:'button',name:'Advance'}}));")
    assert clicked.ok, clicked.model_text
    assert not any(row.call.name == "browser_read" for row in session._memory_results.values())
    read = program(session, tools, snapshot,
        "unwrap(await tools.browser_wait({condition:'text',value:'Completed',timeout_ms:1000}));"
        "return unwrap(await tools.browser_read({}));", turn=2)
    assert read.ok, read.model_text
    assert sum(row.call.name == "browser_click" for row in session._memory_results.values()) == 1
    assert sum(row.call.name == "browser_read" for row in session._memory_results.values()) == 1


def test_record_and_query_targets_use_the_same_structured_source_projection(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession],
) -> None:
    session, tools, browser = production_browser
    assert browser.navigate(URL).ok
    install(browser, '<button aria-controls="result" onclick="document.querySelector(\'#result p\').textContent='
        '\'Actual query value\'">Query</button><article id="result"><h2>Record A</h2><h3>Nested heading</h3>'
        '<p>Previous value</p></article><article><h2>Record B</h2><p>Neighbor value</p></article>')
    page = browser.capture()
    text = page["text"]
    assert isinstance(text, str)
    regions = browser.record_regions(("Record A",))
    assert len(regions) == 1
    target = regions[0]["text"]
    assert isinstance(target, str) and "### Nested heading" in target
    assert SourceReadScope.locate(text, target).start is not None
    snapshot = observed_snapshot(session, browser)
    clicked = program(session, tools, snapshot,
        "return unwrap(await tools.browser_click({ref:{role:'button',name:'Query'},purpose:'result'}));")
    assert clicked.ok, clicked.model_text
    read = next(row for row in session._memory_results.values() if row.call.name == "browser_read")
    assert read.ok and read.audit["query_click"]
    assert "Actual query value" in read.model_text and "Neighbor value" not in read.model_text


@pytest.mark.parametrize("html, readable", [
    ("<p>OK</p>", True), ("<p>已完成</p>", True), ("<p>All Buttons Clicked</p>", True),
    ("<main></main>", False), ("<main>Access denied</main>", False),
    ('<div id="root">Loading...</div>', False),
])
def test_formal_current_page_reads_accept_real_short_messages_but_not_blank_challenge_or_loading_shells(
    production_browser: tuple[TaskSession, ResearchTools, BrowserSession], html: str, readable: bool,
) -> None:
    session, tools, browser = production_browser
    assert browser.navigate(URL).ok
    install(browser, html)
    snapshot = observed_snapshot(session, browser)
    read = program(session, tools, snapshot, "return unwrap(await tools.browser_read({}));")
    assert read.ok is readable, read.model_text
    acquired = [row for row in session._memory_results.values() if row.call.name == "browser_read" and row.ok]
    assert bool(acquired) is readable
