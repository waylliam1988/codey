"""Finding stages reconstructed exclusively from task declarations and receipts."""
from __future__ import annotations

from collections.abc import Mapping

from codey.policies.tool_requirement import ToolSuccessRequirement, parse_tool_success_check
from codey.research.answer_contract import AnswerSessionView
from codey.research.ledger_receipts import source_observation
from codey.runtime.core.execution_record import ExecutionRecord
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object, require_json_integer


def _new_evidence(canonical: Mapping[str, object], seen: dict[tuple[str, str], list[tuple[int, int]]]) -> bool:
    rows = canonical.get("read_passages", [])
    if not isinstance(rows, list) or not rows:
        return False
    source, _, _ = source_observation(canonical.get("research_observation"))
    intervals = seen.setdefault((source.final_url, source.text_hash), [])
    fresh = False
    for value in rows:
        row = decode_json_object(value)
        start, end = require_json_integer(row.get("start"), "start"), require_json_integer(row.get("end"), "end")
        covered = start
        for left, right in sorted(intervals):
            if left <= covered:
                covered = max(covered, right)
        fresh |= covered < end
        intervals.append((start, end))
    return fresh


def comparison_stage(session: AnswerSessionView) -> tuple[tuple[ToolSuccessRequirement, ...], tuple[ToolSuccessRequirement, ...]]:
    rules = tuple(rule for check in session.policy.required_checks
        if (rule := parse_tool_success_check(check)) is not None and rule.tool == "save_finding"
        and set(parse_json_object(rule.arguments_json)) == {"comparison"})
    if not rules:
        return (), ()
    records: dict[str, ExecutionRecord] = {}
    seen: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for ref, record in session.executed.items():
        result = session._memory_results.get(ref)
        if result is not None and result.ok:
            fresh = _new_evidence(result.canonical, seen)
            complete = all(rule.observe(records, results=session._memory_results).evaluation.passed for rule in rules)
            if result.call.name == "research_plan" or complete and fresh:
                records.clear()
        records[ref] = record
    pending = tuple(rule for rule in rules if not rule.observe(records, results=session._memory_results).evaluation.passed)
    return rules, pending


def finding_stage_error(session: AnswerSessionView, args: JsonObject) -> str:
    rules, pending = comparison_stage(session)
    if not rules:
        return ""
    if any(result.ok and result.call.name == "save_finding" and result.call.args == args
           for ref, result in session._memory_results.items() if ref in session.executed):
        return ""
    if not pending:
        return "finding_stage_complete: declared comparisons are already saved; read results or submit the report. Nothing saved."
    if not any(args.get("comparison") == parse_json_object(rule.arguments_json)["comparison"] for rule in pending):
        return "finding_stage_arguments: select an unfinished declared comparison; nothing saved."
    return ""
