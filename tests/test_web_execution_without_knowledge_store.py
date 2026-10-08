"""Web authorization must work when an isolated run has no knowledge vault."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.operations.task_execution import build_research_tools

URL = "https://docs.python.org/3/library/unittest.html"


class Search:
    def search(self, query, limit=5):
        return [{"url": URL, "title": "Testing", "snippet": "Test a discount formula."}]

    def fetch(self, url):
        return {"status": "ok", "url": url, "title": "Testing", "text": "A test checks discounted prices for multiple inputs."}

    def close(self):
        pass


def test_web_resource_factory_does_not_require_or_create_vault(tmp_path):
    search = Search()
    deps = SimpleNamespace(knowledge_store=None, search_factory=lambda: search)
    tools = build_research_tools(deps, session_id="s", project=str(tmp_path))
    assert tools is not None
    assert tools.store is None
    assert "Testing" in tools.web_search("discount test").model_text
    assert list(tmp_path.iterdir()) == []
    for result in [tools.knowledge_search("test").model_text, tools.knowledge_read("missing").model_text,
                   tools.knowledge_write({}).model_text, tools.knowledge_link("a", "b").model_text]:
        assert result.startswith("ERROR:")
        assert "unavailable" in result


def test_web_only_staging_projects_ledger_without_a_dummy_store(tmp_path):
    tools = build_research_tools(SimpleNamespace(knowledge_store=None, search_factory=Search), session_id="s", project="")
    assert tools is not None
    staged = tools.create_staged()
    assert staged.store is None and staged.changes is None
    assert "Testing" in staged.web_search("discount").model_text
    assert staged.ledger is not tools.ledger
    tools.commit_staged(staged)
    assert tools.ledger is staged.ledger
    assert list(tmp_path.iterdir()) == []


@pytest.mark.usefixtures("no_external_advisor_models", "scripted_local_api_connection")
def test_real_headless_hybrid_executes_search_and_open_in_isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    project = tmp_path / "project"
    project.mkdir()
    replies = iter([
        {"tool": "web_search", "args": {"query": "discount testing"}},
        {"tool": "open_url", "args": {"url": URL}},
        {"tool": "done", "args": {"summary": "The opened document explains checking prices with multiple inputs."}},
    ])
    provider = SimpleNamespace(
        name="Local", new_chat=lambda: None, close=lambda: None,
        send=lambda text, timeout=None: json.dumps(next(replies)),
    )
    rows = []
    with (
        mock.patch("codey.research.search_factory.default_research_search_provider", side_effect=Search),
        mock.patch("codey.research.source_gateway.check_fetch_url", return_value=None) as guard,
    ):
        result = run_headless(
            HeadlessRequest(project=project, task="Search the web and open a document about discount tests.",
                            intent="hybrid", provider_id="local", max_turns=4, state_home=tmp_path / "state",
                            sources_open_required=True, project_changes_required=False),
            emit_jsonl=rows.append, connect_provider=lambda *a, **kw: provider,
        )
    assert result.stop_reason == "done", rows
    successful = [row["tool_name"] for row in rows if row.get("type") == "tool" and row.get("ok") is True]
    assert successful == ["web_search", "open_url"]
    guard.assert_called()
    assert not (tmp_path / "state/vault").exists()
