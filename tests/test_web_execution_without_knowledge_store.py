"""Web authorization works when an isolated run has no knowledge vault."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.knowledge.store import KnowledgeStore
from codey.operations.task_execution import build_research_tools
from codey.research.browser_search import BrowserSearchProvider
from codey.types.json_value import JsonObject
from tests.test_research_submission_handoff_production import NativeExperimentProvider, install_http
from tools.completion_explanation_ab import ExperimentProvider, Target

pytestmark = pytest.mark.usefixtures("public_source_dns")

URL = "https://example.com/library/unittest.html"


class Search(BrowserSearchProvider):
    def search(self, query: str, limit: int = 5) -> list[JsonObject]:
        return [{"url": URL, "title": "Testing", "snippet": "Test a discount formula."}]

    def fetch(self, url: str) -> dict[str, object]:
        assert url == URL
        return {"status": "ok", "url": url, "title": "Testing",
                "text": "A test checks discounted prices for multiple inputs."}


@dataclass(frozen=True)
class WebResources:
    search_factory: Callable[[], Search] = Search
    knowledge_store: KnowledgeStore | None = None


def test_web_resource_factory_does_not_require_or_create_vault(tmp_path: Path) -> None:
    tools = build_research_tools(WebResources(), session_id="s", project=str(tmp_path))
    assert tools.store is None
    assert "Testing" in tools.web_search("discount test").model_text
    assert list(tmp_path.iterdir()) == []
    for result in [tools.knowledge_search("test").model_text, tools.knowledge_read("missing").model_text,
                   tools.knowledge_write({}).model_text, tools.knowledge_link("a", "b").model_text]:
        assert result.startswith("ERROR:")
        assert "unavailable" in result


def test_web_only_staging_projects_ledger_without_a_dummy_store(tmp_path: Path) -> None:
    tools = build_research_tools(WebResources(), session_id="s", project="")
    staged = tools.create_staged()
    assert staged.store is None and staged.changes is None
    assert "Testing" in staged.web_search("discount").model_text
    assert staged.ledger is not tools.ledger
    assert tools.ledger.searches == []
    tools.commit_staged(staged)
    assert tools.ledger is staged.ledger
    assert [row.query for row in tools.ledger.searches] == ["discount"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.usefixtures("no_external_advisor_models", "scripted_local_api_connection")
@pytest.mark.parametrize("intent", ["chat", "hybrid"])
@pytest.mark.parametrize("native", [False, True])
def test_real_headless_executes_search_and_open_in_isolated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, intent: str, native: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    replies: list[JsonObject] = [
        {"tool": "web_search", "args": {"query": "discount testing"}},
        {"tool": "open_url", "args": {"url": URL}},
        {"tool": "done", "args": {"summary": "The opened document explains checking prices with multiple inputs."}},
    ]
    install_http(monkeypatch, iter(replies), native=native)
    (tmp_path / "wire").mkdir()
    provider_type = NativeExperimentProvider if native else ExperimentProvider
    provider = provider_type(Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
                                  tmp_path / "wire", fixed_acquisition=False)
    factories: list[Search] = []

    def search_factory() -> Search:
        search = Search()
        factories.append(search)
        return search

    monkeypatch.setattr("codey.research.search_factory.default_research_search_provider", search_factory)
    monkeypatch.setattr("codey.operations.task_phases.dispatch.default_research_search_provider", search_factory)
    rows: list[dict[str, object]] = []

    def connect(provider_id: str, *, port: int) -> ExperimentProvider:
        return provider

    result = run_headless(
        HeadlessRequest(project=project if intent == "hybrid" else None,
                        task="Search the web and open a document about discount tests.",
                        intent=intent, provider_id="local", max_turns=4, state_home=tmp_path / "state",
                        sources_open_required=True, project_changes_required=False),
        emit_jsonl=rows.append, connect_provider=connect,
    )
    assert result.stop_reason == "done", rows
    successful = [row["tool_name"] for row in rows if row.get("type") == "tool" and row.get("ok") is True]
    assert successful == ["web_search", "open_url"]
    assert len(factories) == 1
    assert not (tmp_path / "state/vault").exists()


@pytest.mark.parametrize("failure", ["denied", "empty", "invalid_status"])
def test_failed_source_does_not_become_read_evidence_without_a_vault(tmp_path: Path, failure: str) -> None:
    class FailedSearch(Search):
        def fetch(self, url: str) -> dict[str, object]:
            if failure == "denied":
                return {"status": "error", "url": url, "detail": "HTTP 403", "failure_code": "access_restricted"}
            if failure == "empty":
                return {"status": "ok", "url": url, "title": "Testing", "text": ""}
            return {"status": "invented", "url": url, "text": "An invalid receipt is not evidence."}

    tools = build_research_tools(WebResources(search_factory=FailedSearch), session_id="s", project=str(tmp_path))
    assert "Testing" in tools.web_search("discount test").model_text
    result = tools.open_url(URL)
    assert not result.ok
    assert tools.sources_read == set()
    assert tools.ledger.opened_sources == []
    assert tools.store is None
    assert list(tmp_path.iterdir()) == []
