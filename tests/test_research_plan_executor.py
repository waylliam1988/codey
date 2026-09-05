from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.research.context import ResearchPipelineConfig
from codey.research.plan_executor import PlanExecutor
from codey.research.query_planner import QueryCandidate, ResearchPlan
from codey.research.tools import ResearchTools


def _allow_http_url(url: str, *args, **kwargs) -> str | None:
    del args, kwargs
    if url.startswith(("http://", "https://")):
        return None
    return "only http(s) URLs are allowed"


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

            with mock.patch("codey.research.plan_executor.check_fetch_url", side_effect=_allow_http_url):
                with mock.patch("codey.research.tools.check_fetch_url", side_effect=_allow_http_url):
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


def test_plan_executor_stops_before_search_when_total_source_budget_is_full() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")

        class _TwoHitBackend:
            def __init__(self) -> None:
                self.search_calls: list[tuple[str, int]] = []
                self.fetch_calls: list[str] = []

            def search(self, query: str, limit: int = 8) -> list[dict]:
                self.search_calls.append((query, limit))
                if query == "alpha evidence":
                    return [
                        {"title": "Alpha One", "url": "https://example.com/alpha-1"},
                        {"title": "Alpha Two", "url": "https://example.com/alpha-2"},
                    ]
                if query == "beta evidence":
                    return [{"title": "Beta", "url": "https://example.com/beta"}]
                return []

            def fetch(self, url: str) -> dict:
                self.fetch_calls.append(url)
                return {
                    "url": url,
                    "title": url.rsplit("/", 1)[-1],
                    "text": f"opened body for {url}",
                    "truncated": False,
                }

        backend = _TwoHitBackend()
        try:
            tools = ResearchTools(
                search=backend,
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-budget",
                project="project-budget",
            )
            plan = ResearchPlan(
                plan_ref="research_plan:" + "d" * 16,
                query_candidates=(
                    QueryCandidate("research_query:" + "1" * 16, "alpha evidence"),
                    QueryCandidate("research_query:" + "2" * 16, "beta evidence"),
                ),
                max_queries=2,
                max_sources=2,
            )

            with mock.patch("codey.research.plan_executor.check_fetch_url", side_effect=_allow_http_url):
                with mock.patch("codey.research.tools.check_fetch_url", side_effect=_allow_http_url):
                    result = PlanExecutor(
                        config=ResearchPipelineConfig(
                            max_queries_per_round=2,
                            max_sources_per_query=2,
                            max_total_sources=2,
                        )
                    ).execute(plan, tools)

            assert result.stop_reason == "max_sources"
            assert result.queries_executed == ("alpha evidence",)
            assert backend.search_calls == [("alpha evidence", 8)]
            assert backend.fetch_calls == [
                "https://example.com/alpha-1",
                "https://example.com/alpha-2",
            ]
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

            with mock.patch("codey.research.plan_executor.check_fetch_url", side_effect=_allow_http_url):
                with mock.patch("codey.research.tools.check_fetch_url", side_effect=_allow_http_url):
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


def test_plan_executor_skips_root_landing_pages_before_opening() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")

        class _LandingSearchBackend:
            def __init__(self) -> None:
                self.fetch_calls: list[str] = []

            def search(self, query: str, limit: int = 8) -> list[dict]:
                assert query == "biomedical PubMed evidence"
                return [
                    {
                        "title": "PMC Home",
                        "url": "https://pmc.ncbi.nlm.nih.gov/",
                        "snippet": "PMC home page",
                    },
                    {
                        "title": "PMC article",
                        "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC12064251/",
                        "snippet": "article snippet",
                    },
                ]

            def fetch(self, url: str) -> dict:
                self.fetch_calls.append(url)
                return {
                    "url": url,
                    "title": "PMC article",
                    "text": "opened article body for direct evidence",
                    "truncated": False,
                }

        backend = _LandingSearchBackend()
        try:
            tools = ResearchTools(
                search=backend,
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-landing",
                project="project-landing",
            )
            plan = ResearchPlan(
                plan_ref="research_plan:" + "e" * 16,
                query_candidates=(
                    QueryCandidate("research_query:" + "1" * 16, "biomedical PubMed evidence"),
                ),
                max_queries=1,
                max_sources=2,
            )

            with mock.patch("codey.research.plan_executor.check_fetch_url", side_effect=_allow_http_url):
                with mock.patch("codey.research.tools.check_fetch_url", side_effect=_allow_http_url):
                    result = PlanExecutor(
                        config=ResearchPipelineConfig(
                            max_queries_per_round=1,
                            max_sources_per_query=2,
                            max_total_sources=2,
                        )
                    ).execute(plan, tools)

            assert backend.fetch_calls == ["https://pmc.ncbi.nlm.nih.gov/articles/PMC12064251/"]
            assert result.fresh_source_urls == ("https://pmc.ncbi.nlm.nih.gov/articles/PMC12064251/",)
            assert result.fresh_source_count == 1
            assert result.skipped_count == 1
            assert "low_value_landing_page_url" in result.errors
        finally:
            store.index.close()


def test_plan_executor_does_not_count_redirect_to_root_landing_page_as_fresh_material() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")

        class _RedirectToLandingBackend:
            def __init__(self) -> None:
                self.fetch_calls: list[str] = []

            def search(self, query: str, limit: int = 8) -> list[dict]:
                assert query == "target evidence"
                return [
                    {
                        "title": "Article",
                        "url": "https://example.com/article",
                        "snippet": "article snippet",
                    },
                ]

            def fetch(self, url: str) -> dict:
                self.fetch_calls.append(url)
                return {
                    "url": "https://example.com/",
                    "title": "Example Home",
                    "text": "opened home page body",
                    "truncated": False,
                }

        backend = _RedirectToLandingBackend()
        try:
            tools = ResearchTools(
                search=backend,
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-redirect-landing",
                project="project-redirect-landing",
            )
            plan = ResearchPlan(
                plan_ref="research_plan:" + "f" * 16,
                query_candidates=(
                    QueryCandidate("research_query:" + "1" * 16, "target evidence"),
                ),
                max_queries=1,
                max_sources=1,
            )

            with mock.patch("codey.research.plan_executor.check_fetch_url", side_effect=_allow_http_url):
                with mock.patch("codey.research.tools.check_fetch_url", side_effect=_allow_http_url):
                    result = PlanExecutor(
                        config=ResearchPipelineConfig(
                            max_queries_per_round=1,
                            max_sources_per_query=1,
                            max_total_sources=1,
                        )
                    ).execute(plan, tools)

            assert backend.fetch_calls == ["https://example.com/article"]
            assert result.fresh_source_urls == ()
            assert result.fresh_source_count == 0
            assert result.stop_reason == "no_new_material"
            assert result.skipped_count == 1
            assert any("low_value_landing_page_url after redirect" in error for error in result.errors)
        finally:
            store.index.close()


def test_plan_executor_deduplicates_redirected_fresh_sources() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")

        class _RedirectSearchBackend:
            def __init__(self) -> None:
                self.fetch_calls: list[str] = []

            def search(self, query: str, limit: int = 8) -> list[dict]:
                if query == "first query":
                    return [
                        {"title": "Target HTTP", "url": "http://example.com/target"},
                        {"title": "Target HTTPS", "url": "https://example.com/target"},
                    ]
                if query == "second query":
                    return [
                        {"title": "Target HTTP Again", "url": "http://example.com/target"},
                    ]
                return []

            def fetch(self, url: str) -> dict:
                self.fetch_calls.append(url)
                return {
                    "url": "https://example.com/target",
                    "title": "Target Title",
                    "text": "Target body text",
                    "truncated": False,
                }

            def close(self) -> None:
                pass

        backend = _RedirectSearchBackend()
        try:
            tools = ResearchTools(
                search=backend,
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-redirect",
                project="project-redirect",
            )
            plan = ResearchPlan(
                plan_ref="research_plan:" + "c" * 16,
                query_candidates=(
                    QueryCandidate("research_query:" + "1" * 16, "first query"),
                    QueryCandidate("research_query:" + "2" * 16, "second query"),
                ),
                max_queries=2,
                max_sources=4,
            )

            with mock.patch("codey.research.plan_executor.check_fetch_url", side_effect=_allow_http_url):
                with mock.patch("codey.research.tools.check_fetch_url", side_effect=_allow_http_url):
                    result = PlanExecutor(
                        config=ResearchPipelineConfig(
                            max_queries_per_round=2,
                            max_sources_per_query=4,
                            max_total_sources=4,
                        )
                    ).execute(plan, tools)

            assert result.fresh_source_urls == ("https://example.com/target",)
            assert result.fresh_source_count == 1
            assert len(result.opened_sources) == 1
            # 1st fetch: http://example.com/target -> final https://example.com/target (seen_urls tracks both!)
            # 2nd item https://example.com/target in 1st query is pre-filtered by seen_urls before fetch
            # 2nd query http://example.com/target is pre-filtered by canonical_opened_url before fetch
            assert len(backend.fetch_calls) == 1
            assert result.skipped_count == 2
        finally:
            store.index.close()
