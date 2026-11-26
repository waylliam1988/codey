"""First-party evidence obligations derived from committed source reads."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Protocol

from codey.research.answer_scope import AnswerSessionView
from codey.research.passages import read_passage_refs
from codey.research.source_observation import source_observation
from codey.research.source_trust import primary_source_url
from codey.research.urls import host_key
from codey.runtime.core.models import ToolCall, ToolResult
from codey.task.instruction_text import instruction_text

if TYPE_CHECKING:
    from codey.research.ledger import ResearchLedger

_REQUIRED = re.compile(
    r"\b(?:check|consult|use|cite|verify|read|find|require|against|from)\b[^.!?\n]{0,55}"
    r"\b(?:(?:primary|first[- ]party)\s+(?:sources?|documentation|evidence)"
    r"|official\s+[^.!?\n]{0,96}\b(?:sources?|documentation|docs))\b"
    r"|(?:查阅|查询|查找|核对|使用|引用|提供|依据|基于|需要|要求)[^。！？\n]{0,24}(?:第一方|第一手|一手)(?:资料|来源|文档|证据)",
    re.IGNORECASE,
)
_TERM = re.compile(r"[^\W\d_]{3,}")
_QUESTION_LEAD = re.compile(r"^(?:(?:how|why|when|where)\s+)?(?:would|should|can|could|do)\s+(?:you|I|we)\s+", re.I)
PRIMARY_ACQUISITION_ID = "host:primary-source"
PRIMARY_QUESTION_ID = "host:primary-question"
PRIMARY_ACQUISITION_IDS = frozenset({PRIMARY_ACQUISITION_ID, PRIMARY_QUESTION_ID})


def requires_primary_sources(task: str) -> bool:
    return _REQUIRED.search(instruction_text(task)) is not None


def primary_passage_refs(results: Mapping[str, ToolResult], offered: Iterable[str]) -> tuple[str, ...]:
    qualified: set[str] = set()
    for result in results.values():
        if not result.ok or not result.canonical.get("read_passages"):
            continue
        observation = result.canonical.get("research_observation")
        if not isinstance(observation, dict) or "source" not in observation:
            continue
        source, _, _ = source_observation(observation)
        if primary_source_url(source.final_url):
            qualified.update(read_passage_refs(result.canonical["read_passages"]))
    return tuple(ref for ref in offered if ref in qualified)


class PrimaryAcquisitionView(AnswerSessionView, Protocol):
    @property
    def search_results(self) -> Mapping[str, str]: ...


def primary_acquisition(session: PrimaryAcquisitionView, ledger: ResearchLedger, *,
                        missing_primary: bool, may_search: bool, may_open: bool) -> ToolCall | None:
    from codey.research.questions import requires_question_ref

    identity = PRIMARY_ACQUISITION_ID if missing_primary else PRIMARY_QUESTION_ID
    if (requires_question_ref(session) or any(result.call.call_id == identity
            for result in session._memory_results.values())):
        return None
    if missing_primary and may_open:
        visited = {source.final_url for source in ledger.opened_sources}
        for ref, url in session.search_results.items():
            if url not in visited and primary_source_url(url):
                return ToolCall("open_result", {"result_id": ref}, call_id=PRIMARY_ACQUISITION_ID)
    if may_search and ledger.searches:
        candidate = (ledger.searches[-1].query.strip() + " official documentation" if missing_primary
                     else _unsearched_question(session.task_text, ledger))
        if candidate and len(candidate) <= 500 and not any(
                row.query.casefold() == candidate.casefold() for row in ledger.searches):
            return ToolCall("web_search", {"query": candidate}, call_id=identity)
    return None


def _unsearched_question(task: str, ledger: ResearchLedger) -> str:
    from codey.research.source_connectors import safe_connector_query_terms
    from codey.research.task_requirements import text_units
    from codey.types.json_value import decode_json_object, require_json_string

    hosts = {host_key(source.final_url) for source in ledger.opened_sources if primary_source_url(source.final_url)}
    searched = set(_TERM.findall(" ".join(item.query.casefold() for item in ledger.searches)))
    observed = {term for passage in ledger.read_passages.values() if primary_source_url(passage.source.final_url)
                for term in _TERM.findall(passage.excerpt.casefold())}
    questions = [require_json_string(decode_json_object(row)["text"], "question").strip()
                 for row in text_units(instruction_text(task), sentences=True)]
    questions = [question for question in questions if question.endswith(("?", "？"))]
    if len(questions) < 2:
        return ""
    for question in questions:
        terms = safe_connector_query_terms(_QUESTION_LEAD.sub("", question), limit=10)
        topics = {term.casefold() for term in terms if _TERM.fullmatch(term)}
        if len(topics - searched - observed) > len(topics) // 2:
            return (f"site:{next(iter(hosts))} " if len(hosts) == 1 else "") + question
    return ""
