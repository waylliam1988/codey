"""Conditional source access is authorized without being eagerly executed."""
from __future__ import annotations

import pytest

from codey.automation.browser_session import BrowserSession
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel.kernel_context import KernelContext
from codey.operations.kernel_contracts import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps
from codey.operations.provider_session import adapt_provider
from codey.operations.task_handoff import task_handoff
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.research.tools import ResearchTools
from codey.task.web_intent import explicit_web_intent, source_requirements
from codey.types.json_value import JsonObject
from tests.support.kernel_fixture import runtime

URL = "https://example.com/record"


@pytest.mark.parametrize("task,deferred", [
    (f"请打开 {URL}，查询当前记录。", False),
    (f"请先运行项目检查。需要核对真实记录时，请打开 {URL}，查询当前记录。", True),
    (f"Run local checks first. If needed, open {URL} and inspect the record.", True),
    (f"After checking the project, visit {URL}.", True),
    (f"不要打开 {URL}。", False),
])
def test_conditional_access_keeps_original_source_identity(task: str, deferred: bool) -> None:
    intent = explicit_web_intent(task)
    assert (URL in intent.deferred_urls) is deferred
    if deferred:
        assert URL in intent.urls
        assert source_requirements(task)["required_urls"] == [URL]


def test_kernel_does_not_prefetch_a_deferred_source_before_local_work() -> None:
    class Search:
        def search(self, query: str, *, limit: int) -> list[JsonObject]:
            raise AssertionError("initialization must not search")
        def fetch(self, url: str) -> dict[str, object]:
            raise AssertionError("initialization must not open a conditional URL")
    class Provider:
        def send(self, prompt: str) -> str:
            raise AssertionError("this check does not generate")
    task = f"先运行本地检查。需要核对记录时，请打开 {URL}。"
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"})), task_text=task,
        required_source_urls=frozenset({URL}))
    tools = ResearchTools(Search(), None, None)
    with runtime(task=task) as sink:
        context = KernelContext(session, KernelRunRequest(
            transport=KernelTransportDeps(adapt_provider(Provider()), run_id=sink.run_id),
            execution=KernelExecutionDeps(intent_sink=sink, research_tools=tools)))
        snapshot = build_turn_snapshot(session)
        assert not context.prefetch_required_sources(snapshot)
        assert "open_url" in snapshot.inventory_names
        assert not session.executed


def test_initial_handoff_distinguishes_later_source_obligations_from_prerequisites() -> None:
    task = f"先运行本地检查。需要核对记录时，请打开 {URL}。"
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"})),
        task_text=task, required_source_urls=frozenset({URL}))
    snapshot = build_turn_snapshot(session)
    offered, hint = task_handoff(session, snapshot, CompletionContext(), None)
    assert "stated prerequisite first" in hint and "later evidence obligations" in hint
    assert URL in hint
    assert set(offered.inventory_names) == set(snapshot.inventory_names)
    assert session.required_source_urls == frozenset({URL})
    assert not session.executed


def test_different_page_without_404_receipts_does_not_trigger_navigation_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact"})),
        task_text=f"请打开 {URL}。", required_source_urls=frozenset({URL}))
    snapshot = build_turn_snapshot(session)
    browser = BrowserSession()
    def document() -> JsonObject:
        return {"url": "https://example.com/wrong_record", "loader_id": "wrong"}
    monkeypatch.setattr(browser, "document_identity", document)
    try:
        offered, hint = task_handoff(session, snapshot, CompletionContext(), browser)
        assert '{"args":{"target":"task"},"tool":"browser_navigate"}' not in hint
        assert "Return to the bound task page" not in hint
        assert offered.browser.navigation_url == URL
        assert set(offered.inventory_names) == set(snapshot.inventory_names)
        assert not session.executed
    finally:
        browser.close()
