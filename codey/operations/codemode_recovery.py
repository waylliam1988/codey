"""Resume CodeMode containers through the same execute_turn and Node runtime."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from codey.codemode.identity import parent_execution_id
from codey.codemode.kernel import build_execute_turn_executor
from codey.durable.effects import nested_effect_ids
from codey.durable.model import EffectRecord, thaw_json
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.kernel_session_recovery import restore_task_session
from codey.operations.task_session import TaskSession, turn_effect_id
from codey.runtime.effects.effect_records import RuntimeEffectIntent, RuntimeEffectStore
from codey.runtime.effects.safe_tool_replay import candidate_from_intent

if TYPE_CHECKING:
    from codey.operations.recovery import ResumeRecoveryResult


def recover_containers(
    durable: Any,
    *,
    run_id: str,
    project: str,
    deps: Any,
    mutations: Any,
    pending: tuple[EffectRecord, ...],
    task_kind: str,
    ignored_paths: tuple[str, ...],
) -> ResumeRecoveryResult:
    from codey.operations.recovery import (
        ResumeRecoveryResult,
        _managed_output_store,
        _recover_settled_delivery,
        _settled_facts,
        policy_from_durable_task,
    )
    from codey.operations.task_effects import KernelEffectSink

    if mutations is None:
        raise ValueError("CodeMode recovery requires the original mutation line")
    nested = nested_effect_ids(durable.effects(run_id))
    parents = [record for record in pending if record.intent.get("tool_name") == "codemode"]
    if not parents or any(record not in parents and record.effect_id not in nested for record in pending):
        raise ValueError("CodeMode recovery has unrelated or orphaned pending effects")
    policy = policy_from_durable_task(durable, task_id=run_id)
    session = TaskSession(policy=policy, project=project, task_kind=task_kind)
    session_id = str(parents[0].intent["session_id"])
    projections = RuntimeEffectStore(durable=durable).load_effects(session_id, run_id)
    facts = _settled_facts(
        projections, (), deps=deps, session_id=session_id, run_id=run_id, project=project, ignored_paths=ignored_paths
    )
    # Facts include nested effects; only container receipts become top-level
    # provider deliveries. No nested child is independently dispatched here.
    restore_task_session(
        SimpleNamespace(run_id=run_id, recovered_tool_outcomes=(), settled_tool_outcomes=facts), session
    )
    managed = _managed_output_store(deps)
    from codey.operations.task_changes import task_change_tracker

    tracker = task_change_tracker(deps.state, project, run_id) if policy.allows("project.write") else None
    sink = KernelEffectSink(
        mutations,
        session_id=session_id,
        run_id=run_id,
        provider_id=str(durable.task(run_id).provider_snapshot.get("provider_id", "")),
        managed_outputs=managed,
        durable=durable,
    )
    for record in sorted(parents, key=lambda r: (r.intent["turn"], r.intent["tool_index"])):
        intent = RuntimeEffectIntent.from_payload(cast(dict[str, Any], thaw_json(record.intent)))
        candidate = candidate_from_intent(intent)
        if candidate is None:
            raise ValueError("CodeMode container has no valid original program")
        call = candidate.call
        parent_id = parent_execution_id(run_id, intent.turn, call)
        metadata = durable.task(run_id).state.get("codemode_replay", {}).get(parent_id)
        if metadata is None:
            raise ValueError("CodeMode execution history is missing")
        scope = metadata["effect_scope"]
        identity = turn_effect_id(f"{run_id}:{scope}" if scope else run_id, intent.turn, intent.tool_index)
        if identity != record.effect_id:
            raise ValueError("CodeMode recovery scope differs from original intent")
        snapshot = build_turn_snapshot(session)
        parent = build_execute_turn_executor(
            session=session,
            snapshot=snapshot,
            run_id=run_id,
            effect_scope=scope,
            turn=intent.turn,
            project_path=project,
            change_tracker=tracker,
            managed_outputs=managed,
            session_id=session_id,
            intent_sink=sink,
            durable=durable,
            allow_writes=metadata["allow_writes"],
            workspace_revision_store=getattr(deps, "workspace_revisions", None),
            workspace_ignored_paths=ignored_paths,
        )
        result = execute_turn(
            session,
            [call],
            executors={"codemode": parent},
            run_id=run_id,
            effect_scope=scope,
            turn=intent.turn,
            tool_index_base=intent.tool_index,
            snapshot=snapshot,
            project_path=project,
            change_tracker=tracker,
            managed_outputs=managed,
            session_id=session_id,
            intent_sink=sink,
            durable=durable,
            workspace_revision_store=getattr(deps, "workspace_revisions", None),
            workspace_ignored_paths=ignored_paths,
        )[0]
        if not result.ok:
            raise ValueError(f"CodeMode recovery blocked: {result.model_text}")
    batch = _recover_settled_delivery(durable, mutations=mutations, session_id=session_id, run_id=run_id)
    return ResumeRecoveryResult(ok=True, recovered_tool_result_batch_id=batch)
