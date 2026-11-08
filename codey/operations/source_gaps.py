"""Pure source coverage from admitted requirements and committed observations."""
from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from codey.operations.task_session import TaskSession
from codey.research.ledger import ResearchLedger
from codey.runtime.core.models import ToolResult
from codey.task.web_intent import source_url_key
from codey.types.json_value import JsonObject, decode_json_object


@dataclass(frozen=True)
class SourceFailure:
    url: str
    result_ref: str
    detail: str


@dataclass(frozen=True)
class SourceCoverage:
    required: tuple[str, ...]
    opened: frozenset[str]
    missing: tuple[str, ...]
    failures: tuple[SourceFailure, ...]

    @property
    def satisfied(self) -> bool:
        return not self.missing and not self.failures and bool(self.opened)

    @property
    def unavailable(self) -> bool:
        return bool(self.required and self.failures) and not self.missing

    def facts(self) -> JsonObject:
        return {"required_urls": list(self.required), "opened_urls": [url for url in sorted(self.opened)],
                "unattempted_urls": list(self.missing), "failed_sources": [
                    {"url": row.url, "result_ref": row.result_ref, "detail": row.detail}
                    for row in self.failures]}


def evaluate_source_coverage(*, required_urls: Collection[str], results: Mapping[str, ToolResult],
                             ledger: ResearchLedger | None = None,
                             opened_urls: Collection[str] = ()) -> SourceCoverage:
    """Read facts only; a successful acquisition survives an older/later failure."""
    required = {source_url_key(url) for url in required_urls}
    opened = {source_url_key(url) for url in opened_urls}
    if ledger is not None:
        opened.update(source_url_key(url) for row in ledger.opened_sources
                      for url in (row.requested_url, row.final_url))
    failed: dict[str, SourceFailure] = {}
    for ref, result in results.items():
        observation = result.canonical.get("research_observation")
        if result.ok and isinstance(observation, dict) and isinstance(observation.get("source"), dict):
            source = observation["source"]
            for name in ("requested_url", "final_url"):
                url = source.get(name)
                if isinstance(url, str):
                    opened.add(source_url_key(url))
            # A merged ledger can retain an earlier requested alias. The
            # successful invocation also proves this request reached its source.
            url = result.call.args.get("url")
            if result.call.name == "open_url" and isinstance(url, str):
                opened.add(source_url_key(url))
        failure = result.canonical.get("source_failure")
        if not result.ok and isinstance(failure, dict):
            url, detail = failure.get("url"), failure.get("detail")
            if isinstance(url, str) and isinstance(detail, str):
                key = source_url_key(url)
                failed[key] = SourceFailure(key, ref, detail)
    unresolved = required - opened
    return SourceCoverage(tuple(sorted(required)), frozenset(opened), tuple(sorted(unresolved - failed.keys())),
                          tuple(failed[url] for url in sorted(unresolved & failed.keys())))


def task_source_coverage(session: TaskSession, ledger: ResearchLedger | None = None) -> SourceCoverage:
    return evaluate_source_coverage(required_urls=session.required_source_urls, results=session._memory_results,
                                    ledger=ledger, opened_urls=session.opened_sources)


def source_gap_context(session: TaskSession, ledger: ResearchLedger | None = None) -> str:
    coverage = task_source_coverage(session, ledger)
    if session.task_kind != "chat" or session.policy.strict_research:
        return ""
    if not coverage.required and not coverage.opened:
        return ""
    text = "Source coverage (verified runtime observations):\n" + json.dumps(
        {**coverage.facts(), "source_ids": dict(session.source_ids)}, ensure_ascii=False)
    if coverage.opened:
        text += ("\nOpened sources already delivered page text. Once that text supports the requested answer, "
                 "finish with done in the requested format; do not reopen the same page just to complete. "
                 "If a different passage is needed, use source_search with short relevant terms, then open its "
                 "source offset. A read_tool_result query is one literal substring, not a semantic search.")
    if coverage.missing:
        text += "\nOpen the unattempted required URLs before proposing completion."
    elif coverage.unavailable:
        text += ("\nRequired sources remain unavailable. Deliver the acquired content and explain the actual limitations "
                 "in the requested final format. Proposing done closes as blocked; source_gaps need not be copied. "
                 "Do not repeat an unchanged failing fetch merely to satisfy source-open checks.")
    return text


def validate_source_gaps(session: TaskSession) -> None:
    """Validate optional historical claims; coverage never depends on copying them."""
    raw = session.last_done_args.get("source_gaps")
    if raw is None:
        return
    if session.task_kind != "chat" or session.policy.strict_research:
        raise ValueError("source_gaps is for ordinary chat; keep the task's Research completion contract")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 16:
        raise ValueError("source_gaps requires 1..16 failed source receipt references")
    selected: set[str] = set()
    for item in raw:
        gap = decode_json_object(item)
        if set(gap) != {"result_ref", "reason"} or not isinstance(gap["reason"], str) or not gap["reason"].strip():
            raise ValueError("source_gaps requires result_ref and a nonempty reason")
        ref = gap["result_ref"]
        result = session._memory_results.get(ref) if isinstance(ref, str) else None
        failure = result.canonical.get("source_failure") if result is not None else None
        if result is None or result.ok or not isinstance(failure, dict) or ref in selected:
            raise ValueError("source_gaps must select distinct failed acquisition receipts from this task")
        if not isinstance(failure.get("url"), str) or not failure["url"]:
            raise ValueError("source_gaps receipt lacks its requested URL")
        selected.add(str(ref))
