"""Initial prerequisites and next-step presentation from existing task receipts."""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import replace

from codey.automation.browser_document import same_document
from codey.codemode.catalog import snapshot_catalog
from codey.codemode.json_edit import JSON_FIELD_EDIT_GUIDANCE
from codey.operations.browser_completion import committed_results, complete_page_passages
from codey.operations.browser_followups import (
    DIRECT_TOOLS,
    PENDING,
    UNCERTAIN,
    click_followup_plan,
    codemode_only,
    delivery,
    interrupted_query,
    pending_click,
    unconfirmed_clicks,
)
from codey.operations.browser_references import BrowserReferences
from codey.operations.browser_text_report import MetadataPending, observe_metadata, report_scope
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_output import tool_facts
from codey.operations.kernel_protocol import (
    TurnSnapshot,
    _task_navigation_url,
    offer_browser_call,
    refresh_snapshot_contract,
)
from codey.operations.task_session import TaskSession
from codey.policies.browser_requirement import BrowserPageRounds, parse_browser_requirement
from codey.policies.tool_requirement import (
    ToolObservation,
    ToolSuccessRequirement,
    argument_digest,
    parse_tool_success_check,
)
from codey.runtime.core.models import ToolCall, ToolResult
from codey.task.web_intent import explicit_web_intent
from codey.toolchain.tool_spec import _check_spec_value_against_schema, validate_args_with_spec
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object


def initial_navigation(session: TaskSession, snapshot: TurnSnapshot) -> ToolCall | None:
    """Materialize explicit navigation to one authorized task URL before interaction."""
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
    if not requirements and not explicit_web_intent(session.task_text).browser_navigation_requested:
        return None
    url = _task_navigation_url(session) if requirements else snapshot.task_navigation_url
    if url is None or any(rule.via not in {"", "codemode"} for rule in requirements):
        return None
    args: JsonObject = {"url": url}
    for rule in requirements:
        for key, value in parse_json_object(rule.arguments_json).items():
            if key in args and argument_digest(args[key]) != argument_digest(value):
                return None
            args[key] = value
    if validate_args_with_spec(specs["browser_navigate"], args):
        return None
    if any(rule.via == "codemode" for rule in requirements) or codemode_only(session.task_text):
        if "codemode" not in snapshot.tool_names or "browser_navigate" not in snapshot.nested_tool_names:
            return None
        return ToolCall("codemode", {"code": "return unwrap(await tools.browser_navigate(inputs));", "inputs": args},
                        call_id="required-source:navigation")
    return ToolCall("browser_navigate", args, call_id="required-source:navigation")


def handoff_call(session: TaskSession, snapshot: TurnSnapshot,
                 call: ToolCall) -> tuple[TurnSnapshot, JsonObject | None, str]:
    """Admit a suggested call through the interface actually offered this turn."""
    if call.name not in snapshot.nested_tool_names:
        return snapshot, None, f"{call.name} is unavailable under the current frozen authorization."
    only = codemode_only(session.task_text)
    if not only:
        snapshot = offer_browser_call(snapshot, session, call.name)
    task_selector = call.name == "browser_navigate" and "target" in call.args
    direct = call.name in snapshot.tool_names and not only and not task_selector
    if direct:
        spec = next(spec for spec in snapshot.model_specs if spec.name == call.name)
        missing: list[str] = [name for name in spec.required if name not in call.args]
        error = validate_args_with_spec(spec, call.args)
    else:
        if "codemode" not in snapshot.tool_names:
            return snapshot, None, "CodeMode is unavailable under the current frozen authorization."
        entry = next(entry for entry in snapshot_catalog(snapshot).entries if entry.name == call.name)
        required = entry.parameters.get("required", ())
        missing = [name for name in required if isinstance(name, str) and name not in call.args] if isinstance(required, (list, tuple)) else []
        error = _check_spec_value_against_schema(call.name, "args", entry.parameters, call.args, strict=True)
    if missing:
        return snapshot, None, (f"Use {call.name}; supply {', '.join(missing)} from the task or current page. "
                               f"Known arguments: {json.dumps(call.args, ensure_ascii=False)}. "
                               f"CodeMode wrapper: return unwrap(await tools.{call.name}(inputs));")
    if error:
        return snapshot, None, f"The frozen arguments for {call.name} cannot execute: {error}"
    action: JsonObject = ({"tool": call.name, "args": call.args}
        if direct else
        {"tool": "codemode", "args": {"code": f"return unwrap(await tools.{call.name}(inputs));", "inputs": call.args}})
    return snapshot, action, ""


def _call_handoff(session: TaskSession, snapshot: TurnSnapshot, call: ToolCall) -> tuple[TurnSnapshot, str]:
    snapshot, action, error = handoff_call(session, snapshot, call)
    return snapshot, error if action is None else "Execute this next step: " + json.dumps(action, ensure_ascii=False)


def query_result_handoff(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
                         generation: int | None) -> tuple[TurnSnapshot, str]:
    """Offer task-named controls, without inferring natural-language ordering."""
    if not snapshot.browser_followups or snapshot.input_binding is not None or snapshot.browser_copy is not None:
        return snapshot, ""
    checks = snapshot.policy.required_checks if snapshot.policy else ()
    if "result" not in click_followup_plan(checks, set(snapshot.nested_tool_names)).plans:
        return snapshot, ""
    records, results = tool_facts(session, context)
    if any(same_document(row.audit.get("browser_document"), context.browser_document)
           and delivery(row) in {"verified", "delivered", "uncertain"} for _, row in committed_results(
               records, results, frozenset({"browser_click"}), include_failed=frozenset({"browser_click"}))):
        return snapshot, ""
    if any((rule := parse_tool_success_check(check)) is not None and rule.tool == "browser_fill"
           and not rule.observe(records, results=results, browser_document=context.browser_document).evaluation.passed
           for check in checks):
        return snapshot, ""
    current = _current_snapshot(session, context, generation)
    if current is None:
        return snapshot, ""
    _, observed = current
    contexts = observed.get("contexts")
    labels = {row["id"]: row for row in contexts if isinstance(row, dict)} if isinstance(contexts, list) else {}
    declared = [page_rule.click_name for check in checks
                if isinstance(page_rule := parse_browser_requirement(check), BrowserPageRounds)]
    elements = observed.get("elements")
    lines: list[str] = []
    for row in elements if isinstance(elements, list) else []:
        if not isinstance(row, dict) or row.get("role") not in {"button", "link"} or row.get("disabled") is not False:
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        named = name in declared if declared else re.search(
            r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", session.task_text, re.I) is not None
        if not named:
            continue
        offered, call, _ = handoff_call(session, snapshot, ToolCall("browser_click", {"ref": row["ref"], "purpose": "result"}))
        if call is not None:
            snapshot = offered
            candidate: JsonObject = {"name": name, "context": labels.get(row.get("context_id"), {}), "call": call}
            lines.append(json.dumps(candidate, ensure_ascii=False))
    if not lines:
        return snapshot, ""
    return snapshot, ("When you reach the requested query/detail step, choose its matching current control below. "
                      "The result call clicks once, performs any declared waits and formally reads the result; "
                      "use its child receipts as evidence. Follow the task's ordering and prerequisites.\n" + "\n".join(lines))


def _metadata_handoff(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
                      observed: list[tuple[ToolSuccessRequirement, ToolObservation]]) -> tuple[TurnSnapshot, str]:
    records, results = tool_facts(session, context)
    pending = [(rule, observation) for rule, observation in observed if not observation.evaluation.passed]
    document = context.browser_document
    url = document.get("url") if document is not None else None
    current_read = isinstance(url, str) and any(
        result.audit.get("browser_document") == document and complete_page_passages(result, url)
        for _, result in committed_results(records, results, frozenset({"browser_read"})))
    if (not current_read or pending_click(results, context.browser_document) is not None
            or any(rule.tool not in {"browser_eval", "browser_tab_list"} for rule, _ in pending)):
        return snapshot, ""
    call: ToolCall | None = None
    feedback = ""
    if (scope := report_scope(session)) is not None:
        metadata = observe_metadata(scope, records, results, context.browser_document)
        if isinstance(metadata, MetadataPending):
            call, feedback = metadata.call, metadata.evaluation.feedback
    if call is None and not feedback:
        for rule, observation in pending:
            if rule.tool not in {"browser_eval", "browser_tab_list"}:
                continue
            feedback = observation.evaluation.feedback
            if observation.evaluation.reason_code in {"tool_execution_missing", "observation_selector_unavailable"}:
                declared = next((item for item, _ in pending if item.tool == rule.tool and item.arguments_json != "{}"), rule)
                call = ToolCall(rule.tool, parse_json_object(declared.arguments_json))
            break
    if call is None:
        return snapshot, feedback
    # Only hide redundant outer aliases. The authorized CodeMode leaves
    # remain available for additional task work and page recovery.
    snapshot = refresh_snapshot_contract(replace(snapshot,
        tool_names=tuple(name for name in snapshot.tool_names if name not in DIRECT_TOOLS)))
    snapshot, action = _call_handoff(session, snapshot, call)
    return snapshot, feedback + "\n" + action


def _current_snapshot(session: TaskSession, context: CompletionContext,
                      generation: int | None, *, require_text: bool = True) -> tuple[str, JsonObject] | None:
    records, results = tool_facts(session, context)
    snapshots = committed_results(records, results, frozenset({"browser_snapshot"}))
    raw = snapshots[-1][1].canonical.get("value") if snapshots else None
    value = decode_json_object(raw) if isinstance(raw, dict) else None
    if (value is None or snapshots[-1][1].truncated
            or snapshots[-1][1].audit.get("browser_document") != context.browser_document
            or context.browser_document is None or value.get("generation") != generation
            or (require_text and value.get("complete") is not True) or value.get("elements_truncated") is not False):
        return None
    return snapshots[-1][0], value


def bind_browser_references(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
                            generation: int | None) -> TurnSnapshot:
    """Freeze complete controls independently of body-text truncation."""
    references = None
    if (snapshot.input_binding is None and snapshot.browser_copy is None
            and {"browser_fill", "browser_click"} & set(snapshot.nested_tool_names)
            and context.browser_document is not None
            and (current := _current_snapshot(session, context, generation, require_text=False)) is not None):
        try:
            references = BrowserReferences.observe(current[0], current[1], context.browser_document)
        except ValueError:
            # Older/incomplete receipts cannot authorize descriptive selection.
            references = None
    return refresh_snapshot_contract(replace(snapshot, browser_references=references))


def _action_receipts(session: TaskSession, snapshot: TurnSnapshot,
                     context: CompletionContext) -> list[tuple[str, ToolResult]]:
    records, results = tool_facts(session, context)
    names = frozenset(spec.name for spec in snapshot.frozen_specs
                      if spec.executor == "browser" and spec.effect not in {"read", "navigation"}
                      or spec.executor == "project" and not spec.read_only)
    return [(ref, result) for ref, result in committed_results(records, results, names, include_failed=names)
            if result.ok or delivery(result) in {"verified", "delivered", "uncertain"}
            or records[ref].get("applied_edit") is not None]


def _compile_handoff(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
                     generation: int | None, delivered_results: Sequence[ToolResult]) -> tuple[TurnSnapshot, str]:
    if (snapshot.task_navigation_url is None or context.browser_document is None
            or not any(not result.ok and result.call.name == "codemode"
                       and result.audit.get("error_code") == "syntax_error"
                       and result.canonical.get("child_effect_ids") == [] for result in delivered_results)
            or _action_receipts(session, snapshot, context)):
        return snapshot, ""
    records, results = tool_facts(session, context)
    navigations = committed_results(records, results, frozenset({"browser_navigate"}),
                                    include_failed=frozenset({"browser_navigate"}))
    navigation = navigations[-1][1] if navigations else None
    if (navigation is None or not navigation.ok
            or navigation.call.args.get("url") != snapshot.task_navigation_url
            or not same_document(navigation.audit.get("browser_document"), context.browser_document)):
        return snapshot, ""
    if (current := _current_snapshot(session, context, generation)) is not None:
        ref, value = current
        return snapshot, (f"Reuse the current snapshot from committed receipt {ref}; its controls are still current:\n"
                          + json.dumps(value, ensure_ascii=False))
    snapshot, action = _call_handoff(session, snapshot, ToolCall("browser_snapshot", {}))
    return snapshot, "Compilation failed before any child action. Observe the bound task page next.\n" + action


def _navigation_handoff(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
                        generation: int | None) -> tuple[TurnSnapshot, str]:
    if (snapshot.task_navigation_url is None or context.browser_document is None
            or _action_receipts(session, snapshot, context)):
        return snapshot, ""
    current = _current_snapshot(session, context, generation)
    if current is None or current[1].get("elements") != []:
        return snapshot, ""
    records, results = tool_facts(session, context)
    navigations = committed_results(records, results, frozenset({"browser_navigate"}),
                                    include_failed=frozenset({"browser_navigate"}))
    if not navigations:
        return snapshot, ""
    ref, navigation = navigations[-1]
    requested = navigation.call.args.get("url")
    raw_value = navigation.canonical.get("value")
    value = decode_json_object(raw_value) if isinstance(raw_value, dict) else None
    if (not navigation.ok or navigation.call.call_id.startswith("required-source:")
            or not isinstance(requested, str) or requested == snapshot.task_navigation_url
            or value is None or value.get("http_status") != 404
            or value.get("url") == snapshot.task_navigation_url
            or value.get("url") != context.browser_document.get("url")
            or not same_document(navigation.audit.get("browser_document"), context.browser_document)
            or tuple(records).index(current[0]) <= tuple(records).index(ref)):
        return snapshot, ""
    snapshot, action = _call_handoff(session, snapshot, ToolCall("browser_navigate", {"target": "task"}))
    return snapshot, (f"Navigation receipt {ref} reached HTTP 404 outside the bound task URL, and current snapshot "
                      f"{current[0]} has no controls. Return to the bound task page.\n" + action)


def _download_handoff(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
                      generation: int | None) -> tuple[TurnSnapshot, str, ToolResult | None]:
    if not any((rule := parse_tool_success_check(check)) is not None and rule.tool == "browser_download"
               for check in session.policy.required_checks):
        return snapshot, "", None
    records, results = tool_facts(session, context)
    downloads = committed_results(records, results, frozenset({"browser_download"}),
                                   include_failed=frozenset({"browser_download"}))
    for ref, result in reversed(downloads):
        if result.ok or delivery(result) in {"verified", "delivered", "uncertain"}:
            return snapshot, (f"Download outcome from committed receipt {ref}; reuse it without downloading again:\n"
                              + json.dumps(result.canonical.get("value"), ensure_ascii=False)), result
    current = _current_snapshot(session, context, generation)
    if current is None:
        snapshot, action = _call_handoff(session, snapshot, ToolCall("browser_snapshot", {}))
        return snapshot, action, None
    _, value = current
    elements = value.get("elements")
    candidates: list[JsonObject] = [element for element in elements if isinstance(element, dict)
                  and element.get("role") in {"link", "button"} and element.get("disabled") is False] if isinstance(elements, list) else []
    if not candidates:
        return snapshot, "The current snapshot has no enabled download candidate. Inspect the page before choosing a target.", None
    lines = ["Choose the requested download target from these current snapshot controls:"]
    for item in candidates:
        candidate: JsonObject = {"name": item["name"], "args": {"ref": item["ref"]}}
        lines.append(json.dumps(candidate, ensure_ascii=False))
    if len(candidates) == 1:
        snapshot, action = _call_handoff(session, snapshot, ToolCall("browser_download", {"ref": candidates[0]["ref"]}))
        lines.append(action)
    else:
        snapshot, action = _call_handoff(session, snapshot, ToolCall("browser_download", {}))
        lines.append(action)
    return snapshot, "\n".join(lines), None


def _wait_handoff(session: TaskSession, snapshot: TurnSnapshot,
                  context: CompletionContext) -> tuple[TurnSnapshot, str]:
    records, results = tool_facts(session, context)
    clicks = committed_results(records, results, frozenset({"browser_click"}), include_failed=frozenset({"browser_click"}))
    if (not clicks or not same_document(clicks[-1][1].audit.get("browser_document"), context.browser_document)
            or delivery(clicks[-1][1]) not in {"delivered", "uncertain"}):
        return snapshot, ""
    ref, result = clicks[-1]
    if delivery(result) == "uncertain":
        snapshot, action = _call_handoff(session, snapshot, ToolCall("browser_snapshot", {}))
        return snapshot, UNCERTAIN + "\n" + action
    plan = click_followup_plan(session.policy.required_checks, set(snapshot.nested_tool_names))
    if plan.unavailable:
        return snapshot, "Query follow-up unavailable: " + ", ".join(plan.unavailable) + "."
    keys = tuple(records)
    after = {key: records[key] for key in keys[keys.index(ref) + 1:]
             if records[key]["name"] not in {"browser_wait", "browser_read"}
             or ((receipt := results.get(key)) is not None
                 and same_document(receipt.audit.get("browser_document"), context.browser_document))}
    waits = [rule for check in session.policy.required_checks
             if (rule := parse_tool_success_check(check)) is not None and rule.tool == "browser_wait"]
    waiting = any(not rule.observe(after, results=results, browser_document=context.browser_document).evaluation.passed
                  for rule in waits)
    lines: list[str] = []
    call: ToolCall | None = None
    if waiting:
        lines.append(f"Click {ref} was delivered. Complete the declared browser_wait next; do not click again.")
        steps = plan.plans.get("result")
        for raw in steps if isinstance(steps, list) else []:
            step = decode_json_object(raw)
            args = decode_json_object(step["args"])
            if step["name"] == "browser_wait" and not any(row.ok and row.call.name == "browser_wait"
                    and row.call.args == args for key in after if (row := results.get(key)) is not None):
                call = ToolCall("browser_wait", args)
                break
    elif (any((rule := parse_tool_success_check(check)) is not None and rule.tool == "browser_read"
              for check in session.policy.required_checks)
            and not any(row["name"] == "browser_read" and row["ok"] for row in after.values())):
        lines.append(f"Click {ref} and its declared waits are settled. Use browser_read next; do not click again.")
        call = ToolCall("browser_read", {})
    if plan.missing_waits and waiting:
        lines.append("Automatic result read is unavailable: the wait condition is not fully declared. "
                     "For a query, omit purpose for the atomic browser_click({ref}), then use browser_wait with an explicit "
                     "page condition and timeout_ms, then browser_read. Do not substitute a sleep for a page condition.")
    if call is not None:
        snapshot, action = _call_handoff(session, snapshot, call)
        lines.append(action)
    return snapshot, "\n".join(lines)


def _pending_query_handoff(session: TaskSession, snapshot: TurnSnapshot,
                           context: CompletionContext) -> tuple[TurnSnapshot, str]:
    _, results = tool_facts(session, context)
    click = pending_click(results, context.browser_document)
    if click is not None:
        uncertain = delivery(click[1]) == "uncertain"
        call = ToolCall("browser_snapshot" if uncertain else "browser_read", {})
        if not uncertain:
            plan = click_followup_plan(session.policy.required_checks, set(snapshot.nested_tool_names))
            steps = plan.plans.get("result")
            if isinstance(steps, list):
                refs = tuple(results)
                subsequent = [results[ref] for ref in refs[refs.index(click[0]) + 1:]]
                for raw in steps:
                    step = decode_json_object(raw)
                    args = decode_json_object(step["args"])
                    if step["name"] == "browser_wait" and not any(row.ok and row.call.name == "browser_wait"
                            and row.call.args == args and same_document(row.audit.get("query_wait_document"), context.browser_document)
                            for row in subsequent):
                        call = ToolCall("browser_wait", args)
                        break
        snapshot, action = _call_handoff(session, snapshot, call)
        return snapshot, (UNCERTAIN if uncertain else PENDING) + "\n" + action
    return snapshot, ""


def _project_edit_handoff(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext) -> str:
    tools = {"codemode", "read_file", "edit"}
    if (session.task_kind != "hybrid" or not session.project_changes_required
            or ".json" not in session.task_text.casefold() or "codemode" not in snapshot.tool_names
            or not {"read_file", "edit"} <= set(snapshot.nested_tool_names)
            or tools & set(session.controller_denied)):
        return ""
    records, results = tool_facts(session, context)
    if committed_results(records, results, frozenset({"edit"})):
        return ""
    reads = committed_results(records, results, frozenset({"browser_read"}))
    if not any(not result.truncated and same_document(result.audit.get("browser_document"), context.browser_document)
               for _, result in reads):
        return ""
    return ("Browser evidence is available; the requested project modification still needs an actual edit receipt. "
            "Use the observed field values as inputs.changes and the requested file as inputs.path.\n"
            + JSON_FIELD_EDIT_GUIDANCE)


def browser_task_handoff(session: TaskSession, snapshot: TurnSnapshot, context: CompletionContext,
                          generation: int | None = None, *,
                          delivered_results: Sequence[ToolResult] = ()) -> tuple[TurnSnapshot, str]:
    if (session.task_kind not in {"chat", "hybrid"} or session.policy.strict_research
            or snapshot.input_binding is not None or snapshot.browser_copy is not None
            or "browser_read" not in snapshot.inventory_names):
        return snapshot, ""
    records, results = tool_facts(session, context)
    rules = [rule for check in session.policy.required_checks
             if (rule := parse_tool_success_check(check)) is not None]
    snapshot, pending_query = _pending_query_handoff(session, snapshot, context)
    if pending_query:
        return snapshot, pending_query
    snapshot, waiting = _wait_handoff(session, snapshot, context)
    if waiting:
        return snapshot, waiting
    snapshot, navigation = _navigation_handoff(session, snapshot, context, generation)
    if navigation:
        return snapshot, navigation
    snapshot, download, download_outcome = _download_handoff(session, snapshot, context, generation)
    recovery = ""
    if not download:
        snapshot, recovery = _compile_handoff(session, snapshot, context, generation, delivered_results)
    query = ""
    if not download and not recovery:
        snapshot, query = query_result_handoff(session, snapshot, context, generation)
    interrupted = any(interrupted_query(row, context.browser_document) for _, row in unconfirmed_clicks(results))
    project_edit = _project_edit_handoff(session, snapshot, context)
    if not rules:
        return snapshot, "\n".join(part for part in (recovery, query, project_edit,
            "The previous query document changed; its outcome remains unverified. "
            "Use current-page observations for further work." if interrupted else "") if part)
    observed = [(rule, rule.observe(records, results=results, browser_document=context.browser_document))
                for rule in rules]
    pending = [(rule, outcome.evaluation) for rule, outcome in observed if not outcome.evaluation.passed]
    lines = ["Next step from committed receipts (reuse completed work):"]
    if interrupted:
        lines.append("A previous query was interrupted by a document change; its outcome remains unverified.")
    snapshot, metadata = _metadata_handoff(session, snapshot, context, observed)
    if metadata:
        lines.append(metadata)
    if recovery:
        lines.append(recovery)
    if download:
        lines.append(download)
    if query and not metadata:
        lines.append(query)
    outstanding = [(rule, outcome) for rule, outcome in pending
                   if rule.tool != "browser_download" or download_outcome is None]
    missing_execution = [rule for rule, outcome in outstanding if outcome.reason_code == "tool_execution_missing"]
    if missing_execution:
        lines.append("Outstanding declared calls: " + "; ".join(
            rule.tool + (f" via {rule.via}" if rule.via else "")
            + (f" matching {rule.arguments_json}" if rule.arguments_json != "{}" else "")
            for rule in missing_execution) + ".")
    elif outstanding:
        lines.append("Execution exists but its observation is incomplete. Recover the missing evidence, not the click.")
    elif not pending and not metadata:
        lines.append("All declared tool executions have receipts. If they answer the task, deliver done using those facts.")
    if project_edit:
        lines.append(project_edit)
    return snapshot, "\n".join(line for line in lines if line)
