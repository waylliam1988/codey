"""Match declared browser wait predicates to executable leaf arguments."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from codey.policies.tool_requirement import argument_digest, parse_tool_success_check
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters, thaw_schema_value, validate_args_against_spec
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object

if TYPE_CHECKING:
    from codey.durable.session import DurableSession
    from codey.operations.task_session import TaskSession
    from codey.storage.managed_outputs import ManagedOutputStore


def snapshot_text_baseline(session: TaskSession, result_ref: str) -> tuple[str, JsonObject]:
    """Use the original committed text, including when the worker is restored."""
    from codey.automation.browser_document import same_document
    from codey.operations.browser_completion import committed_results

    snapshots = dict(committed_results(session.executed, session._memory_results, frozenset({"browser_snapshot"})))
    result = snapshots.get(result_ref)
    value = result.canonical.get("value") if result is not None else None
    if result is None or result.truncated or not isinstance(value, dict):
        raise ValueError("text_changed requires a committed successful browser_snapshot receipt")
    value = decode_json_object(value)
    scope, document = value.get("scope"), value.get("document")
    text = value.get("text")
    if (not isinstance(text, str) or value.get("text_truncated") is not False
            or not isinstance(scope, dict) or scope.get("root_ref") != "" or scope.get("selector") != ""
            or not isinstance(document, dict) or not same_document(document, result.audit.get("browser_document"))):
        raise ValueError("text_changed requires complete page text and its original document; scoped or truncated snapshots cannot substitute")
    return text, decode_json_object(document)


def restore_text_wait_snapshot(session: TaskSession, durable: DurableSession, *, owner_task_id: str,
                                result_ref: str, store: ManagedOutputStore | None) -> None:
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
        raise ValueError("text_changed requires an original snapshot owned by the parent task")
    intent = RuntimeEffectIntent.from_payload(canonical_json_object(effect.intent))
    settlement = RuntimeEffectSettlement.from_payload(canonical_json_object(effect.settlement))
    result = restore_result_receipt(RuntimeEffectProjection(intent, settlement), store=store,
        session_id=intent.session_id, run_id=owner_task_id)
    session._memory_results[result_ref] = result
    project_execution_record(session, result_ref, result.call, result, ok=result.ok)
    snapshot_text_baseline(session, result_ref)


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
