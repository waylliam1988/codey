"""Read committed owned-tool admissions, receipts and output."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from codey.durable.model import TASK_TERMINAL_STATUSES, TaskCheckpoint
from codey.durable.session import DurableSession
from codey.durable.tool_task import ToolTaskInput, execution_effect_id
from codey.operations.kernel_receipts import restore_result_receipt
from codey.operations.kernel_result import _error_result
from codey.runtime.core.execution_receipt import execution_receipt
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.effect_records import (
    RuntimeEffectIntent,
    RuntimeEffectProjection,
    RuntimeEffectSettlement,
    compute_args_digest,
)
from codey.storage.execution_output_store import ExecutionOutputStore
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import canonical_json_object, decode_json_object, require_json_integer, require_json_string
from codey.utils.failure_output import failure_excerpt
from codey.workspace.revision import WorkspaceRevisionStore


def owned_tool_task(durable: DurableSession, task_ref: str, *, owner_task_id: str) -> TaskCheckpoint:
    owner = durable.task(owner_task_id)
    child = durable.task(task_ref)
    if (child.execution_kind != "tool" or child.owner_task_id != owner.task_id
            or child.conversation_id != owner.conversation_id or child.submission_id != owner.submission_id):
        raise ValueError("tool task is outside the caller's ownership")
    request = ToolTaskInput.from_payload(child.state["tool_input"])
    origin = durable.effect(request.origin_effect_id)
    if (origin.task_id != owner_task_id
            or origin.intent.get("tool_name") != request.call_name
            or origin.args_digest != compute_args_digest(request.args)):
        raise ValueError("tool task has no committed original admission")
    return child


def tool_execution_result(durable: DurableSession, child: TaskCheckpoint, *, store: ManagedOutputStore | None,
                          project_path: Path | None = None, workspace_store: WorkspaceRevisionStore | None = None,
                          ignored_paths: tuple[str, ...] = ()) -> tuple[str, ToolResult] | None:
    request = ToolTaskInput.from_payload(child.state["tool_input"])
    effects = durable.effects(child.task_id)
    if not effects:
        return None
    if len(effects) != 1 or effects[0].effect_id != execution_effect_id(child.task_id):
        raise ValueError("tool task has conflicting execution effects")
    effect = effects[0]
    if effect.settlement is None:
        return None
    if effect.status == "interrupted":
        return effect.effect_id, _error_result(ToolCall(request.call_name, request.args),
            "execution interrupted; external outcome and cleanup could not be confirmed; not automatically repeated")
    intent_payload: dict[str, object] = dict(canonical_json_object(effect.intent))
    settlement_payload: dict[str, object] = dict(canonical_json_object(effect.settlement))
    projection = RuntimeEffectProjection(RuntimeEffectIntent.from_payload(intent_payload),
        RuntimeEffectSettlement.from_payload(settlement_payload))
    return effect.effect_id, restore_result_receipt(projection, store=store,
        session_id=child.conversation_id, run_id=child.task_id, project_path=project_path,
        workspace_store=workspace_store, ignored_paths=ignored_paths)


@dataclass(frozen=True)
class SettledCheckExecution:
    task_ref: str
    execution_ref: str
    result: ToolResult


def settled_check_for_progress(
    durable: DurableSession, caller_result: ToolResult, *, owner_task_id: str, receipts: Mapping[str, ToolResult],
    include_accepted: bool = False,
) -> SettledCheckExecution | None:
    """Find the original terminal check behind a committed caller delivery."""
    accepted = caller_result.call.name == "run" and caller_result.kind == "accepted"
    if caller_result.call.name == "run" and not accepted:
        reference = caller_result.canonical.get("tool_task")
    elif (accepted and include_accepted) or (_task_observation_ref(caller_result) is not None and caller_result.ok):
        reference = caller_result.canonical.get("value")
    else:
        return None
    if not isinstance(reference, dict) or not isinstance(task_ref := reference.get("task_ref"), str):
        return None
    execution_ref = execution_effect_id(task_ref)
    if not accepted and reference.get("execution_ref") != execution_ref:
        return None
    if caller_result.call.name != "run" and _task_observation_ref(caller_result) != task_ref:
        return None
    try:
        child = owned_tool_task(durable, task_ref, owner_task_id=owner_task_id)
        if not is_settled_project_check(child) or (original := receipts.get(execution_ref)) is None:
            return None
        request = ToolTaskInput.from_payload(child.state["tool_input"])
        if original.call.name != "run" or original.call.args != request.args:
            return None
        if caller_result.call.name == "run" and caller_result.call.args != request.args:
            return None
    except (KeyError, ValueError):
        return None
    return SettledCheckExecution(task_ref, execution_ref, original)


def is_settled_project_check(child: TaskCheckpoint) -> bool:
    from codey.codemode.verification import matches_selected_check, project_check_choices
    from codey.completion.verification_policy import _command_family
    from codey.operations.provider_receipts import restore_snapshot

    if child.status != "completed":
        return False
    request = ToolTaskInput.from_payload(child.state["tool_input"])
    if request.call_name != "run" or request.args.get("tty") is True or request.args.get("stdin") is True:
        return False
    snapshot = restore_snapshot(request.snapshot)
    check = next((spec for spec in snapshot.frozen_specs if spec.name == "run"), None)
    if check is None:
        return False
    if matches_selected_check(check, request.args):
        return True
    command = request.args.get("command")
    family = _command_family(command) if isinstance(command, str) else ""
    if family not in {"python:pytest", "python:unittest"}:
        return False
    return any(isinstance(selected := args.get("command"), str) and _command_family(selected) == family
               and args.get("path", ".") == request.args.get("path", ".")
               for args, _ in project_check_choices(check))


def _task_observation_ref(result: ToolResult) -> str | None:
    if result.call.name in {"task_output", "task_cancel"}:
        ref = result.call.args.get("task_ref")
        return ref if isinstance(ref, str) else None
    ref = result.call.args.get("result_ref")
    return ref if result.call.name == "read_tool_result" and isinstance(ref, str) and ref.startswith("tool_task:") else None


def terminal_tool_task_observations(results: Mapping[str, ToolResult]) -> set[str]:
    """Derive observed terminal states from committed control receipts."""
    observed: set[str] = set()
    for result in results.values():
        forwarded = result.canonical.get("tool_task")
        if result.kind == "completed" and result.call.name == "run" and isinstance(forwarded, dict):
            observed.add(require_json_string(forwarded["task_ref"], "task_ref"))
        if result.ok and _task_observation_ref(result) is not None and isinstance(raw := result.canonical.get("value"), dict):
            value = decode_json_object(raw)
            if value["status"] in TASK_TERMINAL_STATUSES:
                observed.add(require_json_string(value["task_ref"], "task_ref"))
    return observed


def tool_task_snapshot(durable: DurableSession, child: TaskCheckpoint, *, store: ManagedOutputStore | None,
                       args: Mapping[str, object], include_output: bool = True) -> dict[str, object]:
    """One bounded projection shared by explicit reads and automatic notifications."""
    limit = require_json_integer(args.get("limit", 4000), "limit")
    payload: dict[str, object] = {"task_ref": child.task_id, "status": "stopping"
        if child.state.get("cancel_requested") and child.status not in TASK_TERMINAL_STATUSES else str(child.status)}
    request = ToolTaskInput.from_payload(child.state["tool_input"])
    if request.args.get("tty") is True:
        payload["output_mode"] = "terminal"
        payload["streams_merged"] = True
    result = tool_execution_result(durable, child, store=store)
    if result is not None:
        payload["execution_ref"] = result[0]
        shown = result[1].model_text[:limit]
        receipt = execution_receipt(result[1])
        if receipt is not None and receipt.status == "exit":
            excerpt = failure_excerpt(result[1].model_text, limit=limit) if receipt.exit_code != 0 else ""
            if receipt.exit_code == 0 or excerpt:
                shown = excerpt or f"exit {receipt.exit_code}"
        payload["result"] = {"ok": result[1].ok, "text": shown,
                             "text_truncated": shown != result[1].model_text,
                             "exit_code": result[1].audit.get("exit_code")}
    payload["outcome"] = _task_outcome(child, result[1] if result is not None else None)
    if child.state.get("failure_reason"):
        payload["failure_reason"] = require_json_string(child.state["failure_reason"], "failure_reason")[:limit]
    if child.state.get("cleanup_unknown"):
        payload["cleanup_unknown"] = True
    if store is not None and include_output:
        try:
            snapshot = ExecutionOutputStore.read_existing(store.root.parent, execution_effect_id(child.task_id))
        except KeyError:
            snapshot = None
        if snapshot is not None:
            payload["output"] = {channel: ExecutionOutputStore.stream_since(snapshot, channel,
                require_json_integer(args.get(f"{channel}_cursor", 0), f"{channel}_cursor"), limit=limit)
                for channel in ("stdout", "stderr")}
    return payload


def _task_outcome(child: TaskCheckpoint, result: ToolResult | None) -> str:
    if child.status not in TASK_TERMINAL_STATUSES or child.status == "cancelled":
        return str(child.status)
    if child.state.get("cleanup_unknown"):
        return "interrupted"
    if result is None:
        return "failed"
    receipt = execution_receipt(result)
    status = receipt.status if receipt is not None else ""
    if status == "timeout":
        return "timed_out"
    if status in {"output_drain_timeout", "output_read_error"}:
        return "incomplete"
    return "succeeded" if result.ok else "failed"
