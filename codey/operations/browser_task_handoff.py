"""Initial prerequisites and next-step presentation from existing task receipts."""
from __future__ import annotations

from codey.codemode.json_edit import JSON_FIELD_EDIT_GUIDANCE
from codey.operations.browser_followups import PENDING, UNCERTAIN, delivery, pending_click
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_output import tool_facts
from codey.operations.kernel_protocol import TurnSnapshot, _task_navigation_url
from codey.operations.task_session import TaskSession
from codey.policies.browser_requirement import parse_browser_requirement
from codey.policies.tool_requirement import argument_digest, parse_tool_success_check
from codey.runtime.core.models import ToolCall
from codey.toolchain.tool_spec import validate_args_with_spec
from codey.types.json_value import JsonObject, parse_json_object


def initial_navigation(session: TaskSession, snapshot: TurnSnapshot) -> ToolCall | None:
    """Only materialize a declared, uniquely bound prerequisite before interaction."""
    if session.task_kind not in {"chat", "hybrid"} or session.policy.strict_research:
        return None
    # An explicit page sequence already owns its transitions. Do not attach
    # a second opener based only on the accompanying unordered tool checklist.
    if any(parse_browser_requirement(check) is not None for check in session.policy.required_checks):
        return None
    specs = {spec.name: spec for spec in snapshot.frozen_specs}
    if "browser_navigate" not in snapshot.inventory_names:
        return None
    if any((spec := specs.get(result.call.name)) is not None and spec.executor == "browser"
           for result in session._memory_results.values()):
        return None
    requirements = [rule for check in session.policy.required_checks
                    if (rule := parse_tool_success_check(check)) is not None and rule.tool == "browser_navigate"]
    url = _task_navigation_url(session)
    if not requirements or url is None or any(rule.via not in {"", "codemode"} for rule in requirements):
        return None
    args: JsonObject = {"url": url}
    for rule in requirements:
        for key, value in parse_json_object(rule.arguments_json).items():
            if key in args and argument_digest(args[key]) != argument_digest(value):
                return None
            args[key] = value
    if validate_args_with_spec(specs["browser_navigate"], args):
        return None
    if any(rule.via == "codemode" for rule in requirements):
        if "codemode" not in snapshot.tool_names or "browser_navigate" not in snapshot.nested_tool_names:
            return None
        return ToolCall("codemode", {"code": "return unwrap(await tools.browser_navigate(inputs));", "inputs": args},
                        call_id="required-source:navigation")
    return ToolCall("browser_navigate", args, call_id="required-source:navigation")


def browser_task_handoff(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext) -> str:
    if (session.task_kind not in {"chat", "hybrid"} or session.policy.strict_research
            or snapshot.input_binding is not None or snapshot.browser_copy is not None
            or "browser_read" not in snapshot.inventory_names):
        return ""
    records, results = tool_facts(session, context)
    rules = [rule for check in session.policy.required_checks
             if (rule := parse_tool_success_check(check)) is not None]
    if not rules or not results:
        return ""
    observed = [(rule, rule.observe(records, results=results, browser_document=context.browser_document).evaluation)
                for rule in rules]
    pending = [(rule, outcome) for rule, outcome in observed if not outcome.passed]
    missing_execution = [rule for rule, outcome in pending
                         if outcome.reason_code in {"tool_execution_missing", "tool_parent_missing"}]
    lines = ["Next step from committed receipts (reuse completed work):"]
    if (click := pending_click(results)) is not None:
        lines.append(UNCERTAIN if delivery(click[1]) == "uncertain" else PENDING)
    if missing_execution:
        lines.append("Outstanding declared calls: " + "; ".join(
            rule.tool + (f" via {rule.via}" if rule.via else "")
            + (f" matching {rule.arguments_json}" if rule.arguments_json != "{}" else "")
            for rule in missing_execution) + ".")
    elif pending:
        lines.append("Execution exists but its observation is incomplete. Recover the missing evidence, not the click.")
    else:
        lines.append("All declared tool executions have receipts. If they answer the task, deliver done using those facts.")
    if (session.task_kind == "hybrid" and ".json" in session.task_text.casefold()
            and {"read_file", "edit"} <= set(snapshot.nested_tool_names)):
        reads = [result for result in results.values() if result.call.name == "browser_read" and result.ok]
        if reads and not any(result.call.name == "edit" and result.ok for result in results.values()):
            lines.append("Browser evidence is available; the requested project modification still needs an actual edit receipt. "
                         "Use the observed field values as inputs.changes and the requested file as inputs.path.\n"
                         + JSON_FIELD_EDIT_GUIDANCE)
    return "\n".join(lines)
