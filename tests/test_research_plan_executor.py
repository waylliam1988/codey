from __future__ import annotations

import tempfile
from pathlib import Path

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.research.context import ResearchPipelineConfig
from codey.research.plan_executor import PlanExecutor
from codey.research.query_planner import QueryCandidate, ResearchPlan
from codey.research.tools import ResearchTools


class _SearchBackend:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, int]] = []
        self.fetch_calls: list[str] = []
        self.closed = False

    def search(self, query: str, limit: int = 8) -> list[dict]:
        self.search_calls.append((query, limit))
        if query == "alpha evidence":
            return [
                {
                    "title": "Blocked source",
                    "url": "file:///tmp/blocked-alpha.txt",
                    "snippet": "blocked",
                },
                {
                    "title": "Alpha source",
                    "url": "https://example.com/alpha",
                    "snippet": "alpha snippet",
                },
                {
                    "title": "Alpha duplicate",
                    "url": "https://example.com/alpha",
                    "snippet": "alpha duplicate",
                },
            ]
        if query == "beta evidence":
            return [
                {
                    "title": "Beta source",
                    "url": "https://example.com/beta",
                    "snippet": "beta snippet",
                },
            ]
        return []

    def fetch(self, url: str) -> dict:
        self.fetch_calls.append(url)
        title = "Alpha source" if url.endswith("/alpha") else "Beta source" if url.endswith("/beta") else "source"
        return {
            "url": url,
            "title": title,
            "text": f"opened body for {url}",
            "truncated": False,
        }

    def close(self) -> None:
        self.closed = True


def test_plan_executor_bounds_queries_sources_and_url_guard() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        backend = _SearchBackend()
        try:
            tools = ResearchTools(
                search=backend,
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-plan",
                project="project-plan",
            )
            plan = ResearchPlan(
                plan_ref="research_plan:" + "a" * 16,
                query_candidates=(
                    QueryCandidate("research_query:" + "1" * 16, "alpha evidence"),
                    QueryCandidate("research_query:" + "2" * 16, "beta evidence"),
                    QueryCandidate("research_query:" + "3" * 16, "gamma evidence"),
                ),
                max_queries=3,
                max_sources=3,
            )

            result = PlanExecutor(
                config=ResearchPipelineConfig(
                    max_queries_per_round=2,
                    max_sources_per_query=2,
                    max_total_sources=2,
                    max_source_preview_chars=120,
                )
            ).execute(plan, tools)

            assert result.queries_executed == ("alpha evidence", "beta evidence")
            assert len(result.opened_sources) == 2
            assert result.has_new_material is True
            assert result.stop_reason == "max_sources"
            assert result.errors
            assert result.skipped_count >= 1
            assert backend.search_calls == [("alpha evidence", 8), ("beta evidence", 8)]
            assert backend.fetch_calls == [
                "https://example.com/alpha",
                "https://example.com/beta",
            ]
            assert result.previews[0].startswith("query: alpha evidence")
            assert "Alpha source | https://example.com/alpha" in result.previews[0]
            assert "opened body for https://example.com/alpha" in result.previews[0]
            assert "query: beta evidence" in result.previews[1]
            assert "https://example.com/beta" in result.previews[1]
            assert result.fresh_source_urls == ("https://example.com/alpha", "https://example.com/beta")
            assert result.fresh_source_count == 2
        finally:
            store.index.close()


def test_plan_executor_skips_baseline_urls_and_reports_no_new_material() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        backend = _SearchBackend()
        try:
            tools = ResearchTools(
                search=backend,
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-plan",
                project="project-plan",
            )
            tools.sources_read.add("https://example.com/alpha")
            tools.sources_read.add("https://example.com/beta")

            plan = ResearchPlan(
                plan_ref="research_plan:" + "b" * 16,
                query_candidates=(
                    QueryCandidate("research_query:" + "1" * 16, "alpha evidence"),
                ),
                max_queries=1,
                max_sources=2,
            )

            result = PlanExecutor(
                config=ResearchPipelineConfig(
                    max_queries_per_round=1,
                    max_sources_per_query=2,
                    max_total_sources=2,
                )
            ).execute(plan, tools)

            assert result.queries_executed == ("alpha evidence",)
            assert result.fresh_source_urls == ()
            assert result.fresh_source_count == 0
            assert result.has_new_material is False
            assert result.stop_reason == "no_new_material"
            assert "https://example.com/alpha" in result.baseline_source_urls
        finally:
            store.index.close()
