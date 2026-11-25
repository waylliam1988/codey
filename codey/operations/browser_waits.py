"""Match declared browser wait predicates to executable leaf arguments."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from codey.policies.tool_requirement import argument_digest, parse_tool_success_check
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters, thaw_schema_value, validate_args_against_spec
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, parse_json_object

if TYPE_CHECKING:
    from codey.durable.session import DurableSession
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolResult
    from codey.storage.managed_outputs import ManagedOutputStore


def clicked_link_origin(session: TaskSession | None, result_ref: str) -> JsonObject:
    from codey.automation.browser_receipts import committed_results, delivery

    if session is not None:
        clicks = dict(committed_results(session.executed, session._memory_results, frozenset({"browser_click"})))
        click = clicks.get(result_ref)
        if click is not None and delivery(click) == "delivered" and isinstance(origin := click.audit.get("link_origin"), dict):
            return decode_json_object(origin)
    raise ValueError("new_document requires the original delivered link click receipt")


def pending_link_click(session: TaskSession, ref: str, document: JsonObject | None) -> str:
    from codey.automation.browser_document import normalize_element_ref, same_document
    from codey.automation.browser_receipts import committed_results

    waits = committed_results(session.executed, session._memory_results, frozenset({"browser_wait"}),
                              include_failed=frozenset({"browser_wait"}))
    for _, wait in reversed(waits):
        reference = wait.call.args.get("value")
        raw = wait.canonical.get("value")
        if not isinstance(raw, dict):
            continue
        value: Mapping[str, object] = raw
        if (wait.ok or wait.call.args.get("condition") != "new_document" or not isinstance(reference, str)
                or value.get("reason") != "timeout"):
            continue
        click = session._memory_results.get(reference)
        if (reference in session.executed and click is not None and click.ok and click.call.name == "browser_click"
                and isinstance(prior_ref := click.call.args.get("ref"), str)
                and normalize_element_ref(prior_ref) == normalize_element_ref(ref)):
            raw_origin = click.audit.get("link_origin")
            if isinstance(raw_origin, dict):
                origin: Mapping[str, object] = raw_origin
                if document is not None and origin.get("url") == document.get("url") and same_document(raw_origin, document):
                    return "" if observed_link_outcome(session, reference, document) else reference
    return ""


def _modal_ids(result: ToolResult) -> set[str] | None:
    raw = result.canonical.get("value")
    if not isinstance(raw, dict):
        return None
    value: Mapping[str, object] = raw
    scope, contexts = value.get("scope"), value.get("contexts")
    if value.get("elements_truncated") is not False or not isinstance(scope, dict) or not isinstance(contexts, list):
        return None
    bounds: Mapping[str, object] = scope
    if bounds.get("root_ref") != "" or bounds.get("selector") != "":
        return None
    rows: list[object] = contexts
    identities: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            context: Mapping[str, object] = row
            if context.get("kind") in ("dialog", "alertdialog") and isinstance(identity := context.get("id"), str):
                identities.add(identity)
    return identities


def observed_link_outcome(session: TaskSession, click: str, document: JsonObject | None, *,
                          through: str | None = None) -> bool:
    from codey.automation.browser_document import same_document
    from codey.automation.browser_receipts import committed_results
    from codey.operations.browser_completion import PAGE_EVIDENCE_INVALIDATORS

    positions = {ref: index for index, ref in enumerate(session.executed)}
    action = session._memory_results.get(click)
    if action is None or click not in positions or not same_document(action.audit.get("link_origin"), document):
        return False
    action_position = positions[click]
    end = positions.get(through, -1) if through is not None else len(positions)
    if end <= action_position:
        return False
    previous: set[str] | None = None
    for ref, result in committed_results(session.executed, session._memory_results,
                                         PAGE_EVIDENCE_INVALIDATORS | {"browser_snapshot", "browser_read"}):
        position = positions[ref]
        if position > end:
            break
        if position > action_position and result.call.name in PAGE_EVIDENCE_INVALIDATORS:
            return False
        if not same_document(result.audit.get("browser_document"), document):
            continue
        if result.call.name == "browser_read":
            if position > action_position and result.audit.get("query_click") == click:
                return True
        elif (current := _modal_ids(result)) is not None:
            if position < action_position:
                previous = current
            elif previous is not None and current - previous:
                return True
    return False


def _snapshot_baseline(session: TaskSession, result_ref: str) -> tuple[JsonObject, JsonObject]:
    """Read one original committed full-page observation, including after recovery."""
    from codey.automation.browser_document import same_document
    from codey.automation.browser_receipts import committed_results

    snapshots = dict(committed_results(session.executed, session._memory_results, frozenset({"browser_snapshot"})))
    result = snapshots.get(result_ref)
    value = result.canonical.get("value") if result is not None else None
    if result is None or result.truncated or not isinstance(value, dict):
        raise ValueError("snapshot wait requires a committed successful browser_snapshot receipt (text_changed or controls_changed)")
    value = decode_json_object(value)
    scope, document = value.get("scope"), value.get("document")
    if (not isinstance(scope, dict) or scope.get("root_ref") != "" or scope.get("selector") != ""
            or not isinstance(document, dict) or not same_document(document, result.audit.get("browser_document"))):
        raise ValueError("text_changed or controls_changed requires its original full-page document; scoped snapshots cannot substitute")
    return value, decode_json_object(document)


def snapshot_wait_baseline(session: TaskSession, result_ref: str, condition: str) -> tuple[JsonValue, JsonObject]:
    from codey.operations.browser_references import BrowserReferences

    value, document = _snapshot_baseline(session, result_ref)
    if condition == "controls_changed":
        controls = BrowserReferences.observe(result_ref, value, document).controls
        rows: list[JsonValue] = [row.payload() for row in controls]
        return rows, document
    text = value.get("text")
    if condition != "text_changed" or not isinstance(text, str) or value.get("text_truncated") is not False:
        raise ValueError("text_changed requires complete page text; truncated snapshots cannot substitute")
    return text, document


def snapshot_text_baseline(session: TaskSession, result_ref: str) -> tuple[str, JsonObject]:
    from codey.types.json_value import require_json_string

    value, document = snapshot_wait_baseline(session, result_ref, "text_changed")
    return require_json_string(value, "baseline text"), document


def restore_wait_snapshot(session: TaskSession, durable: DurableSession, *, owner_task_id: str,
                          result_ref: str, store: ManagedOutputStore | None, condition: str) -> None:
    """Rebuild the child reader's view from its parent's one authoritative receipt."""
    from codey.operations.kernel_facts import project_execution_record
    from codey.operations.kernel_receipts import restore_result_receipt
    from codey.runtime.effects.effect_records import (
        RuntimeEffectIntent,
        RuntimeEffectProjection,
        RuntimeEffectSettlement,
    )
    from codey.types.json_value import canonical_json_object

    effect = durable.effect(result_ref)
    if (effect.task_id != owner_task_id or effect.category != "tool_call" or effect.settlement is None
            or effect.status != "ok" or effect.intent.get("tool_name") != "browser_snapshot"):
        raise ValueError(f"{condition} requires an original snapshot owned by the parent task")
    intent = RuntimeEffectIntent.from_payload(canonical_json_object(effect.intent))
    settlement = RuntimeEffectSettlement.from_payload(canonical_json_object(effect.settlement))
    result = restore_result_receipt(RuntimeEffectProjection(intent, settlement), store=store,
        session_id=intent.session_id, run_id=owner_task_id)
    session._memory_results[result_ref] = result
    project_execution_record(session, result_ref, result.call, result, ok=result.ok)
    snapshot_wait_baseline(session, result_ref, condition)


@dataclass(frozen=True)
class BrowserWaitPlan:
    waits: tuple[JsonObject, ...]
    missing: tuple[JsonObject, ...]


def complete_wait_args(args: JsonObject, timeout_schema: object = None, *, stage: int = 0,
                       original: JsonObject | None = None) -> JsonObject:
    """Complete only an omitted timeout for an explicit predicate, before admission/replay."""
    if "timeout_ms" in args or not isinstance(args.get("condition"), str) or not args.get("value"):
        return dict(args)
    if original is not None and "timeout_ms" in original:
        return {**args, "timeout_ms": original["timeout_ms"]}
    schema: JsonObject = decode_json_object(thaw_schema_value(timeout_schema)) if timeout_schema is not None else {}
    raw = schema.get("x-codey-declared-waits")
    rows: list[JsonObject] = [decode_json_object(row) for row in raw] if isinstance(raw, list) else []
    matching = [row for row in rows if row.get("stage") in {0, stage}
                and isinstance(row.get("args"), dict) and all(
                    key in args and argument_digest(args[key]) == argument_digest(value)
                    for key, value in decode_json_object(row["args"]).items() if key != "timeout_ms")]
    selected = [row for row in matching if row.get("stage") == stage] or matching
    declared = [decode_json_object(row["args"]) for row in selected]
    timeouts = {timeout for row in declared if type(timeout := row.get("timeout_ms")) is int and all(
        key in args and argument_digest(args[key]) == argument_digest(value)
        for key, value in row.items() if key != "timeout_ms")}
    if len(timeouts) > 1:
        raise ValueError("browser_wait has conflicting declared timeouts; supply timeout_ms explicitly.")
    return {**args, "timeout_ms": next(iter(timeouts), 5000)}


def wait_spec(spec: ToolSpec, checks: tuple[str, ...]) -> ToolSpec:
    declared: list[JsonObject] = [{"stage": rule.stage, "args": parse_json_object(rule.arguments_json)} for check in checks
                if (rule := parse_tool_success_check(check)) is not None and rule.tool == "browser_wait"]
    return freeze_spec_parameters(replace(spec, parameters=tuple(
        (key, {**decode_json_object(thaw_schema_value(schema)), "x-codey-declared-waits": declared})
        if key == "timeout_ms" else (key, schema) for key, schema in spec.parameters)))

def wait_plan(checks: tuple[str, ...], supplied: tuple[JsonObject, ...] = (), *, stage: int = 0) -> BrowserWaitPlan:
    required: list[JsonObject] = []
    waits: list[JsonObject] = []
    for check in checks:
        rule = parse_tool_success_check(check)
        if rule is None or rule.tool != "browser_wait":
            continue
        if rule.stage and rule.stage != stage:
            continue
        expected = parse_json_object(rule.arguments_json)
        required.append(expected)
        args = complete_wait_args(expected)
        if (isinstance(args.get("condition"), str) and isinstance(args.get("value"), str)
                and args["value"] and not validate_args_against_spec("browser_wait", args) and args not in waits):
            waits.append(args)
    for value in supplied:
        args = complete_wait_args(value, {"x-codey-declared-waits": [{"stage": 0, "args": args} for args in required]})
        if error := validate_args_against_spec("browser_wait", args):
            raise ValueError(error)
        if args not in waits:
            waits.append(args)
    missing = tuple(expected for expected in required if not any(all(
        key in wait and argument_digest(wait[key]) == argument_digest(value) for key, value in expected.items())
        for wait in waits))
    return BrowserWaitPlan(tuple(waits), missing)
