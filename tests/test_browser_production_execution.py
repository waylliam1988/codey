"""Browser effects use the production delegate and the ordinary receipt projection."""
from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from playwright.sync_api import Route

from codey.automation.browser_session import BrowserSession
from codey.operations.kernel_facts import bind_source_identity, project_result_observation
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_execution import ExecutionDelegate, build_research_tools
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.browser_search import BrowserSearchProvider
from codey.research.connector_search import ConnectorAwareSearchProvider
from codey.research.ledger import ResearchLedger
from codey.research.tools import ResearchTools
from codey.research.web_sources import WebSourceProvider
from codey.runtime.core.models import ToolCall, ToolResult
from tests.support.real_cdp import chromium_executable

pytestmark = pytest.mark.usefixtures("public_source_dns")
URL = "https://example.com/production-browser-article"
HTML = """<html><body><label>City<input id='city'></label>
<button onclick="document.querySelector('article').textContent='Verified Fuzhou amount is 37 grams.'">Load</button>
<article>Initial article amount is 17 grams.</article></body></html>"""


@pytest.fixture
def production_browser() -> Iterator[tuple[TaskSession, ResearchTools, BrowserSession]]:
    provider = BrowserSearchProvider(headless=True, browser_path=chromium_executable())

    def fixture_route(route: Route) -> None:
        route.fulfill(status=200, content_type="text/html", body=HTML)

    provider._guard_request = fixture_route  # type: ignore[method-assign]
    browser = BrowserSession.from_provider(provider)
    tools = ResearchTools(search=provider, store=None, changes=None, browser_session=browser)
    session = TaskSession(policy=TaskPolicy(grants=frozenset({
        "control", "web.read", "browser.observe", "browser.interact", "browser.effect",
    })), task_kind="chat")
    try:
        yield session, tools, browser
    finally:
        provider.close()


def _delegate(session: TaskSession, tools: ResearchTools) -> ExecutionDelegate:
    snapshot = build_turn_snapshot(session)
    return ExecutionDelegate(session=session, research_tools=tools,
                             frozen_specs={spec.name: spec for spec in snapshot.frozen_specs})


def _ref(delegate: ExecutionDelegate, role: str) -> str:
    result, ok, _ = delegate.execute(ToolCall("browser_snapshot", {}))
    assert ok
    observation = result.canonical["browser_observation"]
    assert isinstance(observation, dict)
    elements = observation["elements"]
    assert isinstance(elements, list)
    return next(str(row["ref"]) for row in elements if isinstance(row, dict) and row["role"] == role)


def _settle(session: TaskSession, tools: ResearchTools, result: ToolResult) -> ToolResult:
    committed = bind_source_identity(session, result.call, result, ok=result.ok)
    project_result_observation(session, result.call, committed, ok=result.ok, research_ledger=tools.ledger)
    return committed


def test_actions_and_reads_share_worker_but_only_committed_reads_establish_evidence(production_browser) -> None:
    session, tools, browser = production_browser
    delegate = _delegate(session, tools)
    nav, ok, _ = delegate.execute(ToolCall("browser_navigate", {"url": URL}))
    assert ok, nav.model_text
    _settle(session, tools, nav)
    ref = _ref(delegate, "textbox")
    filled, ok, _ = delegate.execute(ToolCall("browser_fill", {"ref": ref, "value": "Fuzhou"}))
    assert ok
    assert not tools.ledger.opened_sources
    assert "read_passages" not in filled.canonical

    # The next turn constructs a new delegate over the SAME session resource.
    delegate = _delegate(session, tools)
    assert delegate.browser_session is browser
    ref = _ref(delegate, "button")
    clicked, ok, _ = delegate.execute(ToolCall("browser_click", {"ref": ref}))
    assert ok
    assert "37 grams" in clicked.model_text
    assert not tools.ledger.opened_sources
    stale, ok, _ = delegate.execute(ToolCall("browser_click", {"ref": ref}))
    assert not ok
    assert stale.canonical["browser_observation"]["reason"] == "stale_ref"

    read, ok, _ = delegate.execute(ToolCall("browser_read", {}))
    assert ok, read.model_text
    assert "37 grams" in read.model_text
    assert read.canonical["read_passages"]
    assert not tools.ledger.opened_sources  # Observation is not a second write.
    committed = _settle(session, tools, read)
    assert committed.canonical["source_id"] == "s1"
    assert session.opened_sources == {URL}
    assert tools.ledger.read_passages

    restored = ResearchLedger()
    replay = TaskSession(policy=session.policy, task_kind="chat")
    project_result_observation(replay, committed.call, committed, ok=True, research_ledger=restored)
    assert restored.read_passages == tools.ledger.read_passages
    assert replay.source_ids == session.source_ids


def test_eval_is_visible_action_output_and_invalidates_refs_without_citations(production_browser) -> None:
    session, tools, _ = production_browser
    delegate = _delegate(session, tools)
    delegate.execute(ToolCall("browser_navigate", {"url": URL}))
    ref = _ref(delegate, "button")
    evaluated, ok, _ = delegate.execute(ToolCall("browser_eval", {"code": "'EVAL_TOKEN_42'"}))
    assert ok and "EVAL_TOKEN_42" in evaluated.model_text
    _settle(session, tools, evaluated)
    assert not tools.ledger.read_passages
    assert not delegate.execute(ToolCall("browser_click", {"ref": ref}))[1]


def test_browser_actions_cannot_smuggle_source_evidence(production_browser) -> None:
    session, tools, _ = production_browser
    forged = ToolResult(ok=True, call=ToolCall("browser_snapshot", {}), model_text="fake",
                        canonical={"read_passages": [{"ref": "fake"}]})
    with pytest.raises(ValueError, match="browser actions cannot establish source evidence"):
        project_result_observation(session, forged.call, forged, ok=True, research_ledger=tools.ledger)


def test_successful_browser_read_requires_passages() -> None:
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"browser.observe"})), task_kind="chat")
    result = ToolResult(ok=True, call=ToolCall("browser_read", {}), model_text="fake")
    with pytest.raises(ValueError, match="actual read passages"):
        project_result_observation(session, result.call, result, ok=True, research_ledger=ResearchLedger())


def test_missing_browser_resource_cannot_fall_back_to_custom_executor() -> None:
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"browser.observe"})), task_kind="chat")

    def forbidden(_call: ToolCall) -> ToolResult:
        raise AssertionError("built-in browser must not use a custom executor")

    delegate = ExecutionDelegate(session=session, custom_executors={"browser_snapshot": forbidden})
    assert delegate.handles("browser_snapshot")
    result, ok, _ = delegate.execute(ToolCall("browser_snapshot", {}))
    assert not ok and "session unavailable" in result.model_text


def test_browser_resource_cannot_grant_unauthorized_actions(production_browser) -> None:
    _, tools, _ = production_browser
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"browser.observe"})), task_kind="chat")
    result, ok, _ = ExecutionDelegate(session=session, research_tools=tools).execute(
        ToolCall("browser_navigate", {"url": URL}))
    assert not ok and "denied" in result.model_text


def test_default_provider_wrapper_shares_one_browser_without_starting_chromium() -> None:
    base = BrowserSearchProvider(headless=True)
    web = WebSourceProvider(browser_factory=lambda: base)
    provider = ConnectorAwareSearchProvider(web)
    try:
        resources = build_research_tools(SimpleNamespace(search_factory=lambda: provider),
                                         session_id="s", project="", browser_actions=True)
        assert resources.browser_session is not None
        assert resources.browser_session.provider is base
        assert web.browser_source is base
        assert base._session is None
        assert resources.create_staged().browser_session is resources.browser_session
    finally:
        provider.close()
