"""Bounded execution for deterministic Research plans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from codey.runtime import cancellation
from codey.research.context import ResearchPipelineConfig
from codey.utils.refs import clip
from codey.research.query_planner import ResearchPlan
from codey.research.tools import ResearchTools, clone_research_tools
from codey.research.url_selection import source_candidate_skip_reason
from codey.policies.network import check_fetch_url


@dataclass(frozen=True)
class PlanExecutionResult:
    queries_executed: tuple[str, ...] = ()
    opened_sources: tuple[dict, ...] = ()
    previews: tuple[str, ...] = ()
    fresh_source_urls: tuple[str, ...] = ()
    fresh_source_count: int = 0
    baseline_source_urls: tuple[str, ...] = ()
    skipped_count: int = 0
    stop_reason: str = ""
    errors: tuple[str, ...] = ()

    @property
    def has_new_material(self) -> bool:
        return bool(self.fresh_source_urls)


class PlanExecutor:
    def __init__(
        self,
        *,
        config: ResearchPipelineConfig | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.config = config or ResearchPipelineConfig()
        self.should_stop = should_stop or (lambda: False)

    def execute(self, plan: ResearchPlan, tools: ResearchTools) -> PlanExecutionResult:
        runtime = clone_research_tools(tools)
        queries: list[str] = []

        opened: list[dict] = []
        previews: list[str] = []
        fresh_urls: list[str] = []
        errors: list[str] = []
        skipped = 0
        baseline_urls = _collect_baseline_urls(tools)
        seen_urls: set[str] = set(baseline_urls)
        stop_reason = "no_queries"
        query_limit = _bounded_int(
            min(int(plan.max_queries or 0), self.config.max_queries_per_round),
            default=self.config.max_queries_per_round,
            lower=0,
            upper=8,
        )
        total_limit = _bounded_int(
            min(int(plan.max_sources or 0), self.config.max_total_sources),
            default=self.config.max_total_sources,
            lower=0,
            upper=12,
        )
        per_query_limit = _bounded_int(
            self.config.max_sources_per_query,
            default=2,
            lower=0,
            upper=8,
        )
        for candidate in plan.query_candidates[:query_limit]:
            if self._is_stopped():
                stop_reason = "stopped"
                break
            if len(opened) >= total_limit or total_limit <= 0 or per_query_limit <= 0:
                stop_reason = "max_sources"
                break
            query = " ".join(str(candidate.query_preview or "").split())
            if not query:
                skipped += 1
                continue
            before_searches = len(runtime.ledger.searches)
            result = runtime.web_search(query)
            queries.append(query)
            if str(result or "").startswith("ERROR:"):
                errors.append(_safe_error(result))
                stop_reason = "search_error"
                continue
            search = runtime.ledger.searches[-1] if len(runtime.ledger.searches) > before_searches else None
            if search is None:
                stop_reason = "no_results"
                continue
            opened_for_query = 0
            for hit in search.results:
                if len(opened) >= total_limit:
                    stop_reason = "max_sources"
                    break
                if opened_for_query >= per_query_limit:
                    break
                url = str(hit.url or "").strip()
                if not url or url in seen_urls:
                    skipped += 1
                    continue
                skip_reason = source_candidate_skip_reason(url)
                if skip_reason:
                    seen_urls.add(url)
                    skipped += 1
                    errors.append(_safe_error(skip_reason))
                    continue
                pre_canonical = runtime.ledger.canonical_opened_url(url)
                if pre_canonical and (pre_canonical in seen_urls or pre_canonical in baseline_urls or pre_canonical in fresh_urls):
                    seen_urls.add(url)
                    skipped += 1
                    continue
                seen_urls.add(url)
                reason = check_fetch_url(url)

                if reason:
                    skipped += 1
                    errors.append(_safe_error(reason))
                    continue
                before_opened = set(runtime.ledger.final_url_set())
                try:
                    body = runtime.open_url(
                        url,
                        limit=self.config.max_source_preview_chars,
                    )
                except cancellation.TaskCancelled:
                    raise
                if self._is_stopped():
                    stop_reason = "stopped"
                    break
                text = str(body or "")
                if text.startswith(("ERROR:", "SKIPPED:")):
                    skipped += 1
                    errors.append(_safe_error(text))
                    continue
                after_opened = set(runtime.ledger.final_url_set())
                new_urls = sorted(after_opened - before_opened)
                canonical_final = (
                    runtime.ledger.canonical_opened_url(new_urls[-1])
                    if new_urls
                    else runtime.ledger.canonical_opened_url(url)
                ) or url
                seen_urls.add(canonical_final)
                final_skip_reason = source_candidate_skip_reason(canonical_final)
                if final_skip_reason:
                    skipped += 1
                    errors.append(_safe_error(final_skip_reason + " after redirect"))
                    continue
                if canonical_final in baseline_urls or canonical_final in fresh_urls:
                    skipped += 1
                    continue
                opened_for_query += 1
                source = _opened_source_payload(runtime, canonical_final)
                if source:
                    opened.append(source)
                fresh_urls.append(canonical_final)
                previews.append(_source_preview(query, source, text, self.config.max_source_preview_chars))
                stop_reason = "opened_sources"

            if stop_reason in {"max_sources", "stopped"}:
                break
        if stop_reason == "opened_sources" and len(opened) >= total_limit:
            stop_reason = "max_sources"
        if stop_reason in {"no_queries", "opened_sources"} and queries and not fresh_urls:
            stop_reason = "no_new_material"
        return PlanExecutionResult(
            queries_executed=tuple(queries),
            opened_sources=tuple(opened),
            previews=tuple(previews),
            fresh_source_urls=tuple(fresh_urls),
            fresh_source_count=len(fresh_urls),
            baseline_source_urls=tuple(sorted(baseline_urls)),
            skipped_count=skipped,
            stop_reason=stop_reason,
            errors=tuple(errors[:12]),
        )

    def _is_stopped(self) -> bool:
        if self.should_stop():
            return True
        cancellation.check()
        return False



def _collect_baseline_urls(tools: ResearchTools) -> set[str]:
    baseline: set[str] = set()
    for url in tools.sources_read:
        if str(url or "").strip():
            baseline.add(str(url).strip())
    for url in tools.ledger.final_url_set():
        if str(url or "").strip():
            baseline.add(str(url).strip())
    for opened in tools.ledger.opened_sources:
        if str(opened.final_url or "").strip():
            baseline.add(str(opened.final_url).strip())
        if str(opened.requested_url or "").strip():
            baseline.add(str(opened.requested_url).strip())
    for ev in tools.ledger.evidence_items:
        if str(ev.source_url or "").strip():
            baseline.add(str(ev.source_url).strip())
    return baseline


def _opened_source_payload(tools: ResearchTools, url: str) -> dict:
    final_url = tools.ledger.canonical_opened_url(url) or str(url or "")
    for item in tools.ledger.opened_sources:
        if item.final_url == final_url or item.requested_url == final_url:
            return item.to_dict()
    return {}


def _source_preview(query: str, source: dict, body: str, limit: int) -> str:
    title = str(source.get("title") or "").strip()
    final_url = str(source.get("final_url") or source.get("url") or "").strip()
    header = " | ".join(part for part in (title, final_url) if part)
    text = clip(str(body or ""), max(500, min(4000, int(limit or 0))))
    return "\n".join(part for part in (f"query: {query}", header, text) if part)


def _safe_error(value: object) -> str:
    return clip(" ".join(str(value or "").split()), 180)


def _bounded_int(value: object, *, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(lower, min(upper, parsed))


__all__ = [
    "PlanExecutionResult",
    "PlanExecutor",
]
