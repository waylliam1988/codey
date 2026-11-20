"""Rebuild verification order from the original committed effects, never arrival order."""
from __future__ import annotations

from pathlib import Path

from codey.durable.session import DurableSession
from codey.operations.kernel_facts import project_result_observation
from codey.operations.kernel_receipts import restore_result_receipt
from codey.operations.task_session import TaskSession
from codey.runtime.effects.effect_records import RuntimeEffectIntent, RuntimeEffectProjection, RuntimeEffectSettlement
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import canonical_json_object
from codey.workspace.revision import WorkspaceRevisionStore


def settled_projections(durable: DurableSession, owner_task_id: str) -> tuple[RuntimeEffectProjection, ...]:
    from codey.operations.tool_tasks import owned_tool_task

    rows: list[RuntimeEffectProjection] = []
    for effect in durable.settled_effects(owner_task_id):
        if effect.category not in {"tool_call", "provider_send"} or effect.settlement is None:
            continue
        if effect.task_id != owner_task_id:
            owned_tool_task(durable, effect.task_id, owner_task_id=owner_task_id)
        intent: dict[str, object] = dict(canonical_json_object(effect.intent))
        settlement: dict[str, object] = dict(canonical_json_object(effect.settlement))
        rows.append(RuntimeEffectProjection(
            RuntimeEffectIntent.from_payload(intent), RuntimeEffectSettlement.from_payload(settlement)))
    return tuple(rows)


def rebuild_verifications(session: TaskSession, *, durable: DurableSession, owner_task_id: str,
                          store: ManagedOutputStore | None, project_path: Path | None,
                          workspace_store: WorkspaceRevisionStore | None = None,
                          ignored_paths: tuple[str, ...] = ()) -> None:
    """Replace only the derived verification view, preserving every unrelated fact."""
    from codey.operations.tool_tasks import owned_tool_task, tool_execution_result

    scratch = TaskSession(session.policy, task_kind=session.task_kind, project=session.project)
    for effect in settled_projections(durable, owner_task_id):
        if effect.intent.effect_category != "tool_call" or effect.intent.tool_name not in {"run", "edit"}:
            continue
        if effect.intent.run_id != owner_task_id:
            child = owned_tool_task(durable, effect.intent.run_id, owner_task_id=owner_task_id)
            if effect.settlement is not None and effect.settlement.status == "interrupted":
                restored = tool_execution_result(durable, child, store=store)
                if restored is None:
                    continue
                result = restored[1]
            else:
                result = restore_result_receipt(effect, store=store, session_id=child.conversation_id,
                    run_id=child.task_id, project_path=project_path, workspace_store=workspace_store,
                    ignored_paths=ignored_paths)
        else:
            result = restore_result_receipt(effect, store=store, session_id=effect.intent.session_id,
                run_id=owner_task_id, project_path=project_path, workspace_store=workspace_store,
                ignored_paths=ignored_paths)
        project_result_observation(scratch, result.call, result, ok=result.ok,
            exit_code=effect.settlement.exit_code if effect.settlement is not None else None)
    session.verifications = scratch.verifications
