"""Derive a declared wait/read continuation from settled current-page effects."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from codey.automation.browser_document import same_document
from codey.operations.browser_completion import (
    PAGE_ACTIONS,
    PAGE_EVIDENCE_INVALIDATORS,
    browser_click_stage,
    committed_results,
    complete_page_passages,
    ordered_browser_progress,
    post_wait_passages,
    wait_matched_on_page,
)
from codey.operations.browser_followups import UNCERTAIN, click_followup_plan, delivery, pending_click
from codey.policies.browser_requirement import BrowserResearchSequence, parse_browser_requirement
from codey.policies.tool_requirement import argument_digest, parse_tool_success_check
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, parse_json_object


@dataclass(frozen=True)
class BrowserNextStep:
    call: ToolCall | None
    feedback: str


def _matching_waits(args: JsonObject, rows: list[tuple[str, ToolResult]], url: str) -> list[int]:
    return [index for index, (_, row) in enumerate(rows) if row.ok and row.call.name == "browser_wait"
            and wait_matched_on_page(row, url) and all(
                key in row.call.args and argument_digest(row.call.args[key]) == argument_digest(value)
                for key, value in args.items())]


def next_browser_step(checks: tuple[str, ...], *, records: Mapping[str, ExecutionRecord],
                      results: Mapping[str, ToolResult], document: JsonObject | None,
                      names: set[str], workflow: bool = False) -> BrowserNextStep | None:
    if document is None or not isinstance(url := document.get("url"), str):
        return None
    active = tuple(check for check in checks if (page := parse_browser_requirement(check)) is None or page.url == url)
    pages = tuple(page for check in active if isinstance(page := parse_browser_requirement(check), BrowserResearchSequence))
    rules = tuple(rule for check in active if (rule := parse_tool_success_check(check)) is not None)
    stage = browser_click_stage(records, results, document)
    earlier = [rule for rule in rules if rule.stage and rule.stage < stage and not rule.observe(
        records, results=results, browser_document=document).evaluation.passed]
    if earlier:
        return BrowserNextStep(None, "An earlier declared browser stage lacks settled evidence. "
            "Recover that stage on its original document before advancing; its waits cannot attach to the current click.")
    invalidators = PAGE_EVIDENCE_INVALIDATORS if pages or workflow else PAGE_ACTIONS
    actions = [(ref, row) for ref, row in committed_results(records, results, invalidators, include_failed=invalidators)
               if same_document(row.audit.get("browser_document"), document)]
    query = pending_click(results, document)
    clicks = [(ref, row) for ref, row in actions if row.call.name == "browser_click"
              and (row.ok or delivery(row) in {"delivered", "uncertain"})]
    if clicks and delivery(clicks[-1][1]) == "uncertain":
        ref = clicks[-1][0]
        keys = tuple(records)
        inspected = any(row.ok and row.call.name == "browser_snapshot"
                        and same_document(row.audit.get("browser_document"), document)
                        for key in keys[keys.index(ref) + 1:] if (row := results.get(key)) is not None)
        return BrowserNextStep(None if inspected else ToolCall("browser_snapshot", {}), UNCERTAIN)
    if not (query or clicks or pages or workflow or any(rule.after for rule in rules)):
        return None
    applicable = tuple(check for check in active if (rule := parse_tool_success_check(check)) is None
                       or not rule.stage or rule.stage == stage)
    current_rules = tuple(rule for check in applicable if (rule := parse_tool_success_check(check)) is not None)
    if not (query or clicks or pages or workflow or any(rule.after for rule in current_rules)):
        return None
    for rule in current_rules:
        if rule.tool != "browser_wait" or not rule.after:
            continue
        progress = ordered_browser_progress(rule.tool, rule.after, records=records, results=results,
                                             document=document, stage=rule.stage)
        if progress.uncertain:
            keys = tuple(records)
            after = keys[keys.index(actions[-1][0]) + 1:] if actions else keys
            inspected = any(row.ok and row.call.name == "browser_snapshot"
                            and same_document(row.audit.get("browser_document"), document)
                            for key in after if (row := results.get(key)) is not None)
            return BrowserNextStep(None if inspected else ToolCall("browser_snapshot", {}), UNCERTAIN)
        if not progress.ready:
            return None
    plan = click_followup_plan(applicable, names, stage=stage)
    if plan.unavailable:
        return BrowserNextStep(None, "Query follow-up unavailable: " + ", ".join(plan.unavailable) + ".")
    keys = tuple(records)
    baseline = actions[-1][0] if actions else query[0] if query is not None else None
    remaining = keys[keys.index(baseline) + 1:] if baseline is not None else keys
    current = [(ref, results[ref]) for ref in remaining if ref in results
               and same_document(results[ref].audit.get("browser_document"), document)]
    declared_waits = tuple(parse_json_object(rule.arguments_json) for rule in rules
                          if rule.tool == "browser_wait" and rule.stage in {0, stage})
    latest_wait = -1
    for expected in plan.missing_waits:
        matching = _matching_waits(expected, current, url)
        if not matching:
            return BrowserNextStep(None, ("The click was delivered. Complete the declared browser_wait next. "
                if clicks or query is not None else "")
                + "Automatic result read is unavailable: the wait condition is not fully declared. "
                "Use browser_wait with an explicit page condition and timeout_ms, then browser_read. "
                "Do not substitute a sleep for a page condition.")
        latest_wait = max(latest_wait, matching[-1])
    for args in plan.waits:
        expected = dict(args)
        if not any("timeout_ms" in required and all(
                key in args and argument_digest(args[key]) == argument_digest(value)
                for key, value in required.items()) for required in declared_waits):
            del expected["timeout_ms"]
        matching = _matching_waits(expected, current, url)
        if not matching:
            feedback = ("The click was delivered. Complete the declared browser_wait next on the current document; "
                        "do not click again." if clicks or query is not None else
                        "Complete the declared browser_wait next on the current document.")
            return BrowserNextStep(ToolCall("browser_wait", args),
                                   feedback)
        latest_wait = max(latest_wait, matching[-1])
    needs_read = query is not None or bool(pages) or any(rule.tool == "browser_read" and rule.stage in {0, stage} for rule in rules)
    read = any(row.ok and row.call.name == "browser_read" and complete_page_passages(row, url)
               for _, row in current[latest_wait + 1:])
    page_ready = all(bool(post_wait_passages(page, records=records, results=results, document=document)[1]) for page in pages)
    if needs_read and (not read or not page_ready):
        return BrowserNextStep(ToolCall("browser_read", {}),
            "The declared waits are settled. Use browser_read on the same document for complete text; do not click again.")
    return None
