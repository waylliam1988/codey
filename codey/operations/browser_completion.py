"""Evaluate explicitly declared browser prerequisites from committed receipts."""
from __future__ import annotations

from collections.abc import Mapping

from codey.policies.browser_requirement import BrowserResearchSequence
from codey.policies.requirement import RequirementEvaluation
from codey.policies.tool_requirement import _browser_body
from codey.research.answer_contract import AnswerScope
from codey.research.answer_report import AnswerReport
from codey.research.finding_report import ResearchReport
from codey.research.ledger_receipts import source_observation
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolResult
from codey.types.json_value import JsonObject


def committed_results(records: Mapping[str, ExecutionRecord], results: Mapping[str, ToolResult],
                      names: frozenset[str]) -> list[tuple[str, ToolResult]]:
    return [(ref, result) for ref, row in records.items() if row["name"] in names and row["ok"] is True
            and (result := results.get(ref)) is not None and result.ok and result.call.name == row["name"]
            and result.call.call_id == row["call_id"]]


def matches_wait(result: ToolResult, *, url: str, condition: str, value: str) -> bool:
    observed = result.canonical.get("value")
    return (result.call.args.get("condition") == condition and result.call.args.get("value") == value
            and isinstance(observed, dict) and observed.get("matched") is True and observed.get("page_url") == url)


def complete_page_passages(result: ToolResult, url: str) -> set[str]:
    passages = result.canonical.get("read_passages")
    if not isinstance(passages, list):
        return set()
    try:
        source, _, _ = source_observation(result.canonical.get("research_observation"))
        if source.final_url != url or not _browser_body(result):
            return set()
    except (KeyError, TypeError, ValueError):
        return set()
    return {ref for item in passages if isinstance(item, dict) and isinstance(ref := item.get("ref"), str)}


def evaluate_research_sequence(rule: BrowserResearchSequence, *, records: Mapping[str, ExecutionRecord],
                               results: Mapping[str, ToolResult], document: JsonObject | None,
                               report: ResearchReport | None) -> RequirementEvaluation:
    if document is None or document.get("url") != rule.url:
        return RequirementEvaluation("browser_navigation_missing", f"Use browser_navigate to open the required page {rule.url}.")
    wait = ""
    passages: dict[str, tuple[str, str]] = {}
    for ref, result in committed_results(records, results, rule.receipt_tools):
        if result.audit.get("browser_document") != document:
            continue
        if result.call.name == "browser_wait" and matches_wait(result, url=rule.url, condition=rule.condition, value=rule.value):
            wait = ref
        elif wait and result.call.name == "browser_read":
            passages.update((passage, (wait, ref)) for passage in complete_page_passages(result, rule.url))
    if not wait:
        return RequirementEvaluation("browser_wait_missing", f"On the required current page, {rule.wait_step}")
    if not passages:
        return RequirementEvaluation("browser_read_after_wait_missing",
            "The required wait succeeded. Use browser_read on the same page to obtain complete text, then cite that read.", (wait,))

    cited: set[str] = set()
    target_passages: set[str] = set()
    if isinstance(report, AnswerReport):
        cited = {row["passage_ref"] for row in report.citation_bindings}
        target_sources = {(row["id"], row["revision"]) for row in report.source_versions if row["url"] == rule.url}
        target_passages = {row["passage_ref"] for row in report.evidence_bindings
                           if (row["source_id"], row["source_revision"]) in target_sources and "passage_ref" in row}
    fresh = cited.intersection(passages)
    if fresh and not (cited & target_passages) - passages.keys():
        return RequirementEvaluation(evidence_refs=tuple(dict.fromkeys(ref for key in sorted(fresh) for ref in passages[key])))

    submissions = committed_results(records, results, frozenset({"submit_research_answer"}))
    raw = submissions[-1][1].canonical.get("research_submission") if submissions else None
    scope = AnswerScope.from_payload(raw["scope"]) if isinstance(raw, dict) else None
    labels = ", ".join(f"[{label}]" for label, ref in scope.aliases if ref in passages) if scope is not None else ""
    return RequirementEvaluation("browser_post_wait_citation_missing",
        "The post-wait text is already available. Use submit_research_answer and cite "
        + (labels or "the evidence from that read") + " for the required page.",
        tuple(dict.fromkeys(ref for pair in passages.values() for ref in pair)))
