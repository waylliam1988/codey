"""Evaluate explicitly declared browser prerequisites from committed receipts."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from codey.automation.browser_session import normalize_element_ref
from codey.policies.browser_requirement import PAGE_TRANSITIONS, BrowserPageRounds, BrowserResearchSequence
from codey.policies.requirement import RequirementEvaluation
from codey.policies.tool_requirement import _browser_body
from codey.research.answer_contract import AnswerScope
from codey.research.answer_report import AnswerReport
from codey.research.finding_report import ResearchReport
from codey.research.ledger_receipts import source_observation
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolResult
from codey.types.json_value import JsonObject

PAGE_EVIDENCE_TOOLS = frozenset({"browser_wait", "browser_read"})
_FORM_ACTIONS = frozenset({"browser_fill", "browser_click"})


def committed_results(records: Mapping[str, ExecutionRecord], results: Mapping[str, ToolResult],
                      names: frozenset[str], *, include_failed: frozenset[str] = frozenset()) -> list[tuple[str, ToolResult]]:
    return [(ref, result) for ref, row in records.items() if row["name"] in names
            and (row["ok"] or row["name"] in include_failed) and row.get("execution_disposition") != "denied_before_execution"
            and (result := results.get(ref)) is not None and result.ok == row["ok"] and result.call.name == row["name"]
            and result.call.call_id == row["call_id"]]


def matches_wait(result: ToolResult, *, url: str, condition: str, value: str) -> bool:
    return (result.call.args.get("condition") == condition and result.call.args.get("value") == value
            and wait_matched_on_page(result, url))


def wait_matched_on_page(result: ToolResult, url: str) -> bool:
    observed = result.canonical.get("value")
    if not isinstance(observed, dict):
        return False
    value = dict[str, object](observed)
    return value.get("matched") is True and value.get("page_url") == url


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


def post_wait_passages(rule: BrowserResearchSequence, *, records: Mapping[str, ExecutionRecord],
                       results: Mapping[str, ToolResult], document: JsonObject) -> tuple[str, dict[str, tuple[str, str]]]:
    return ordered_page_passages(rule.url, records=records, results=results, document=document,
        wait_matches=lambda _, result: matches_wait(result, url=rule.url, condition=rule.condition, value=rule.value))


def ordered_page_passages(url: str, *, records: Mapping[str, ExecutionRecord], results: Mapping[str, ToolResult],
                          document: JsonObject, wait_matches: Callable[[str, ToolResult], bool],
                          read_refs: frozenset[str] | None = None,
                          invalidated_by: frozenset[str] = frozenset()) -> tuple[str, dict[str, tuple[str, str]]]:
    wait = ""
    passages: dict[str, tuple[str, str]] = {}
    for ref, result in committed_results(records, results, PAGE_EVIDENCE_TOOLS | invalidated_by,
                                         include_failed=invalidated_by):
        if result.audit.get("browser_document") != document:
            continue
        if result.call.name in invalidated_by:
            wait = ""
            passages.clear()
        elif result.call.name == "browser_wait" and wait_matches(ref, result):
            wait = ref
        elif wait and result.call.name == "browser_read" and (read_refs is None or ref in read_refs):
            passages.update((passage, (wait, ref)) for passage in complete_page_passages(result, url))
    return wait, passages


def evaluate_research_sequence(rule: BrowserResearchSequence, *, records: Mapping[str, ExecutionRecord],
                               results: Mapping[str, ToolResult], document: JsonObject | None,
                               report: ResearchReport | None) -> RequirementEvaluation:
    if document is None or document.get("url") != rule.url:
        return RequirementEvaluation("browser_navigation_missing", f"Use browser_navigate to open the required page {rule.url}.")
    wait, passages = post_wait_passages(rule, records=records, results=results, document=document)
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
        return RequirementEvaluation(evidence_refs=tuple({ref: None for key in sorted(fresh) for ref in passages[key]}))

    submissions = committed_results(records, results, frozenset({"submit_research_answer"}))
    raw = submissions[-1][1].canonical.get("research_submission") if submissions else None
    scope = AnswerScope.from_payload(dict[str, object](raw)["scope"]) if isinstance(raw, dict) else None
    labels = ", ".join(f"[{label}]" for label, ref in scope.aliases if ref in passages) if scope is not None else ""
    return RequirementEvaluation("browser_post_wait_citation_missing",
        "The post-wait text is already available. Use submit_research_answer and cite "
        + (labels or "the evidence from that read") + " for the required page.",
        tuple({ref: None for pair in passages.values() for ref in pair}))


_ROUND_STEPS = ("browser_navigate", "browser_snapshot", "browser_fill", "browser_snapshot",
                "browser_click", "browser_wait", "browser_read")


def _navigation_document(result: ToolResult, url: str) -> JsonObject | None:
    raw_value = result.canonical.get("value")
    raw_document = result.audit.get("browser_document")
    if not isinstance(raw_value, dict) or not isinstance(raw_document, dict):
        return None
    value = dict[str, object](raw_value)
    document = dict[str, object](raw_document)
    if (result.call.name == "browser_navigate" and result.ok and result.call.args.get("url") == url
            and value.get("url") == url
            and type(status := value.get("http_status")) is int and 200 <= status < 400
            and document.get("url") == url
            and isinstance(loader := document.get("loader_id"), str) and loader):
        return {"url": url, "loader_id": loader}
    return None


def _snapshot_ref(result: ToolResult, label: str, url: str) -> str:
    raw = result.canonical.get("value")
    if not isinstance(raw, dict):
        return ""
    value = dict[str, object](raw)
    if (not result.ok or result.truncated or value.get("page_url") != url
            or value.get("elements_truncated") is not False or not isinstance(items := value.get("elements"), list)):
        return ""
    rows = (dict[str, object](item) for item in list[object](items) if isinstance(item, dict))
    refs = [row.get("ref") for row in rows if row.get("name") == label]
    return refs[0] if len(refs) == 1 and isinstance(refs[0], str) else ""


@dataclass
class _PageRound:
    document: JsonObject
    refs: list[str]
    target_ref: str = ""

    def observe(self, rule: BrowserPageRounds, ref: str, result: ToolResult) -> None:
        name = result.call.name
        index = len(self.refs)
        step = _ROUND_STEPS[index]
        if name == "browser_snapshot" and index in {1, 2, 3, 4}:
            self._snapshot(rule, ref, result)
        elif name in _FORM_ACTIONS:
            self._form_action(rule, ref, result, step)
        elif name == step and result.ok and (
            (name == "browser_wait" and matches_wait(result, url=rule.url, condition=rule.condition, value=rule.value))
            or (name == "browser_read" and complete_page_passages(result, rule.url))
        ):
            self.refs.append(ref)

    def _snapshot(self, rule: BrowserPageRounds, ref: str, result: ToolResult) -> None:
        index = len(self.refs)
        label = rule.fill_name if index <= 2 else rule.click_name
        if index in {2, 4}:
            self.refs.pop()
        self.target_ref = _snapshot_ref(result, label, rule.url)
        if self.target_ref:
            self.refs.append(ref)

    def _form_action(self, rule: BrowserPageRounds, ref: str, result: ToolResult, step: str) -> None:
        if step in {"browser_wait", "browser_read"}:
            del self.refs[1:]
            return
        if step not in _FORM_ACTIONS:
            return
        raw = result.canonical.get("value")
        value = dict[str, object](raw) if isinstance(raw, dict) else {}
        delivered = (value.get("ref") == self.target_ref
                     and (value.get("delivery") == "verified" and result.call.args.get("value") == rule.fill_value
                          if step == "browser_fill" else value.get("delivery") == "delivered"))
        requested_ref = result.call.args.get("ref")
        if (result.ok and result.call.name == step and isinstance(requested_ref, str)
                and normalize_element_ref(requested_ref) == self.target_ref and delivered):
            self.refs.append(ref)
        else:
            self.refs.pop()


def evaluate_page_rounds(rule: BrowserPageRounds, *, records: Mapping[str, ExecutionRecord],
                         results: Mapping[str, ToolResult]) -> RequirementEvaluation:
    evidence: list[str] = []
    completed_documents: list[JsonObject] = []
    current: _PageRound | None = None
    for ref, result in committed_results(records, results, rule.receipt_tools,
                                         include_failed=PAGE_TRANSITIONS | _FORM_ACTIONS | {"browser_snapshot"}):
        if result.call.name in PAGE_TRANSITIONS:
            document = _navigation_document(result, rule.url)
            current = _PageRound(document, [ref]) if document is not None and document not in completed_documents else None
        elif current is not None:
            if result.audit.get("browser_document") != current.document:
                current = None
                continue
            current.observe(rule, ref, result)
            if len(current.refs) == len(_ROUND_STEPS):
                completed_documents.append(current.document)
                evidence.extend(current.refs)
                current = None
                if len(completed_documents) == rule.rounds:
                    return RequirementEvaluation(evidence_refs=tuple(evidence))
    index = len(current.refs) if current is not None else 0
    advice = (f"Use browser_navigate to open {rule.url}.", f"Use browser_snapshot to find {rule.fill_name!r}.",
              f"Use browser_fill on {rule.fill_name!r} with value {rule.fill_value!r}.",
              f"Use a fresh browser_snapshot to find {rule.click_name!r}.", f"Use browser_click on {rule.click_name!r}.",
              rule.wait_step, "Use browser_read to obtain the complete post-wait text.")
    return RequirementEvaluation(f"browser_round_missing_{_ROUND_STEPS[index]}",
        f"Completed {len(completed_documents)} of {rule.rounds} browser rounds. Round {len(completed_documents) + 1}: {advice[index]}",
        tuple(evidence) + (tuple(current.refs) if current is not None else ()))
