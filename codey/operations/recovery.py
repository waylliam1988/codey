"""Resume recovery for pending runtime effects."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from codey.agents.request import RecoveredToolOutcome
from codey.agents.tool_execution import (
    evaluate_tool_call_policy_for,
    execute_information_tool_call,
    policy_denied,
)
from codey.agents.tools import DEFAULT_TOOL_FNS
from codey.policies.permissions import profile_for_task_kind
from codey.runtime.core import cancellation
from codey.runtime.core.operation_reducer import (
    ACTION_CONTINUE,
    ACTION_FAIL_INVARIANT,
    ACTION_REDELIVER_SETTLED_BATCH,
    ACTION_REPLAY_SAFE_TOOL_BATCH,
    ACTION_SETTLE_PROVIDER_UNKNOWN,
    ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS,
    ACTION_TERMINAL,
    RuntimeAction,
)
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    SENT_STATE_MAYBE_SENT,
    SENT_STATE_SETTLED,
    SETTLEMENT_STATUS_ERROR,
    SETTLEMENT_STATUS_OK,
    RuntimeEffectProjection,
    RuntimeEffectSettlement,
)
from codey.runtime.effects.replay_policy import ReplayClass
from codey.runtime.effects.safe_tool_replay import (
    SafeToolReplayCandidate,
    candidate_from_intent,
)
from codey.runtime.write.drive import peek_next_action
from codey.runtime.write.mutation_line import RuntimeMutationLine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResumeRecoveryResult:
    ok: bool
    recovered_tool_outcomes: tuple[RecoveredToolOutcome, ...] = ()
    recovered_tool_result_batch_id: str = ""
    settled_tool_outcomes: tuple[RecoveredToolOutcome, ...] = ()


def recover_effects_for_resume(
    deps: Any,
    *,
    session_id: str,
    run_id: str,
    project: str,
    task_kind: str,
    ignored_paths: tuple[str, ...] = (),
) -> ResumeRecoveryResult:
    """Recover pending delivery and project all settled facts independently.

    A delivered batch stops needing provider messages, but its observations
    remain part of the task on every restart. No executor runs while loading
    settled facts, and an unavailable receipt never becomes an empty task.
    """
    pending = _recover_pending_effects(
        deps, session_id=session_id, run_id=run_id, project=project,
        task_kind=task_kind, ignored_paths=ignored_paths,
    )
    if not pending.ok:
        return pending
    store = _runtime_effect_store(deps)
    if store is None:
        return pending
    try:
        facts = _settled_facts(
            store.load_effects(session_id, run_id), pending.recovered_tool_outcomes,
            deps=deps, session_id=session_id, run_id=run_id, project=project,
            ignored_paths=ignored_paths,
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return ResumeRecoveryResult(ok=False)
    return replace(pending, settled_tool_outcomes=facts)


def _settled_facts(
    projections: tuple[RuntimeEffectProjection, ...], pending_rows: tuple[RecoveredToolOutcome, ...], *,
    deps: Any, session_id: str, run_id: str, project: str, ignored_paths: tuple[str, ...],
) -> tuple[RecoveredToolOutcome, ...]:
    pending_by_id = {row.effect_id: row for row in pending_rows}
    rows: list[RecoveredToolOutcome] = []
    for projection in projections:
        intent = projection.intent
        if intent.effect_category != EFFECT_CATEGORY_TOOL_CALL or not projection.is_settled:
            continue
        row = pending_by_id.get(intent.effect_id)
        if row is None:
            result = rebuild_settled_tool_result(
                projection, managed_store=_managed_output_store(deps),
                session_id=session_id, run_id=run_id,
                project_path=Path(project) if project else None,
                workspace_store=getattr(deps, "workspace_revisions", None),
                ignored_paths=ignored_paths,
            )
            if result is None:
                raise ValueError("settled task fact has no valid receipt")
            row = _redelivered_outcome(
                projection, result, turn=intent.turn, tool_index=intent.tool_index,
                effect_id=intent.effect_id,
            )
            if row is None:
                raise ValueError("settled task fact cannot be reconstructed")
        rows.append(row)
    return tuple(rows)


def _recover_pending_effects(
    deps: Any,
    *,
    session_id: str,
    run_id: str,
    project: str,
    task_kind: str,
    ignored_paths: tuple[str, ...] = (),
) -> ResumeRecoveryResult:
    effects_store = _runtime_effect_store(deps)
    delivery_store = _tool_result_delivery_store(deps)
    mutations = _runtime_mutations(deps)
    if mutations is None:
        return ResumeRecoveryResult(ok=True)
    try:
        action = peek_next_action(
            mutations.session_log,
            session_id=session_id,
            run_id=run_id,
        )
    except Exception:
        return ResumeRecoveryResult(ok=False)
    if action.kind == ACTION_FAIL_INVARIANT:
        return ResumeRecoveryResult(ok=False)
    if action.kind in {ACTION_CONTINUE, ACTION_TERMINAL}:
        return ResumeRecoveryResult(ok=True)
    if effects_store is None:
        return ResumeRecoveryResult(ok=False)
    try:
        all_projections = effects_store.load_effects(session_id, run_id)
    except Exception:
        return ResumeRecoveryResult(ok=False)

    project_path = _writer_project_path(project, task_kind)
    profile_name = profile_for_task_kind(task_kind, phase="writer").name if project_path is not None else ""

    if action.kind == ACTION_SETTLE_PROVIDER_UNKNOWN:
        projection = _projection_for_effect(all_projections, action.effect_id)
        if projection is None:
            return ResumeRecoveryResult(ok=False)
        if not _settle_interrupted(
            mutations,
            session_id=session_id,
            run_id=run_id,
            effect_id=projection.intent.effect_id,
            effect_category=projection.intent.effect_category,
            replay_class=projection.intent.replay_class,
        ):
            return ResumeRecoveryResult(ok=False)
        return ResumeRecoveryResult(ok=True)

    if action.kind == ACTION_REPLAY_SAFE_TOOL_BATCH:
        return _replay_safe_tools_for_action(
            mutations,
            action,
            all_projections,
            delivery_store=delivery_store,
            session_id=session_id,
            run_id=run_id,
            project_path=project_path,
            profile_name=profile_name,
            managed_store=_managed_output_store(deps),
            workspace_store=getattr(deps, "workspace_revisions", None),
            ignored_paths=ignored_paths,
        )

    if action.kind == ACTION_REDELIVER_SETTLED_BATCH:
        # 已结算重发：重建原结果交付，不调用执行器，不依赖 ReplayClass.SAFE。
        return _redeliver_settled_batch_for_action(
            action,
            all_projections,
            delivery_store=delivery_store,
            mutations=mutations,
            session_id=session_id,
            run_id=run_id,
            project_path=Path(project) if project else None,
            managed_store=_managed_output_store(deps),
            workspace_store=getattr(deps, "workspace_revisions", None),
            ignored_paths=ignored_paths,
        )

    if action.kind == ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS:
        for effect_id in action.effect_ids:
            projection = _projection_for_effect(all_projections, effect_id)
            if projection is None:
                return ResumeRecoveryResult(ok=False)
            if not _settle_interrupted(
                mutations,
                session_id=session_id,
                run_id=run_id,
                effect_id=projection.intent.effect_id,
                effect_category=projection.intent.effect_category,
                replay_class=projection.intent.replay_class,
            ):
                return ResumeRecoveryResult(ok=False)
        return ResumeRecoveryResult(ok=True)

    return ResumeRecoveryResult(ok=False)


def _runtime_mutations(deps: Any) -> RuntimeMutationLine | None:
    # None means "no durable runtime attached (e.g. chat-only), nothing to
    # recover", not "recovery succeeded despite failure". Callers distinguish
    # via ResumeRecoveryResult.ok.
    return getattr(deps, "runtime_mutations", None)


def _runtime_effect_store(deps: Any) -> Any:
    return getattr(deps, "runtime_effects", None)


def _tool_result_delivery_store(deps: Any) -> Any:
    store = getattr(deps, "tool_result_delivery", None)
    if store is not None:
        return store
    state = getattr(deps, "state", None)
    return getattr(state, "tool_result_delivery", None)


def _managed_output_store(deps: Any) -> Any:
    store = getattr(deps, "managed_outputs", None)
    if store is not None:
        return store
    state = getattr(deps, "state", None)
    return getattr(state, "managed_outputs", None)


def _writer_project_path(project: str, task_kind: str) -> Path | None:
    if task_kind not in {"project", "hybrid"} or not project:
        return None
    path = Path(project).expanduser().resolve()
    return path if path.is_dir() else None


def _try_replay_safe_tool(
    mutations: RuntimeMutationLine,
    candidate: SafeToolReplayCandidate | None,
    *,
    session_id: str,
    run_id: str,
    project_path: Path | None,
    profile_name: str,
    tool_fns: Any,
    settle: bool = True,
    managed_store: Any = None,
) -> RecoveredToolOutcome | None:
    if candidate is None or project_path is None:
        logger.warning(
            "safe replay skipped: run=%s effect=%s tool=%s reason=%s",
            run_id,
            getattr(candidate, "effect_id", ""),
            getattr(getattr(candidate, "call", None), "name", ""),
            "no_candidate_or_project",
        )
        return None
    try:
        policy_decision, replay_decision = evaluate_tool_call_policy_for(
            candidate.call,
            project=project_path,
            permission_profile=profile_name,
            approval_available=False,
            phase="writer",
        )
        if policy_denied(policy_decision) or replay_decision.replay_class != ReplayClass.SAFE:
            logger.warning(
                "safe replay denied: run=%s effect=%s tool=%s reason=%s",
                run_id,
                candidate.effect_id,
                candidate.call.name,
                "policy_or_replay_class",
            )
            return None
        outcome = execute_information_tool_call(project_path, tool_fns, candidate.call)
        status = SETTLEMENT_STATUS_OK if outcome.ok else SETTLEMENT_STATUS_ERROR
        error_code = str(outcome.error_code or ("" if outcome.ok else "error"))[:80]
        if settle:
            # 新读取作为新的观察结算（含完整有界结果与退出码），不冒充旧结果
            from codey.operations.kernel_receipts import result_receipt_fields
            from codey.runtime.core.models import ToolResult

            exit_code = outcome.exit_code if type(outcome.exit_code) is int else None
            result = ToolResult(
                ok=outcome.ok, call=candidate.call, model_text=outcome.model_text,
                audit=outcome.audit, canonical=outcome.canonical,
                presentation=outcome.presentation, truncated=outcome.truncated,
            )
            fields = result_receipt_fields(
                result, store=managed_store, session_id=session_id,
                run_id=run_id, effect_id=candidate.effect_id,
            )
            mutations.settle_tool_effect(
                session_id,
                run_id,
                RuntimeEffectSettlement(
                    effect_id=candidate.effect_id,
                    effect_category=EFFECT_CATEGORY_TOOL_CALL,
                    session_id=session_id,
                    run_id=run_id,
                    status=status,
                    error_code=error_code,
                    sent_state=SENT_STATE_SETTLED,
                    replay_class=ReplayClass.SAFE,
                    replay_count=1,
                    replayed_from_effect_id=candidate.effect_id,
                    **fields,
                    exit_code=exit_code,
                ),
            )
        return RecoveredToolOutcome(
            call=candidate.call,
            outcome=outcome,
            turn=candidate.turn,
            tool_index=candidate.tool_index,
            effect_id=candidate.effect_id,
        )
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception as exc:
        # One bounded diagnostic with IDs only; never log arg bodies.
        logger.warning(
            "safe replay failed: run=%s effect=%s tool=%s reason=%s",
            run_id,
            getattr(candidate, "effect_id", ""),
            getattr(getattr(candidate, "call", None), "name", ""),
            type(exc).__name__[:40],
        )
        return None


def rebuild_settled_tool_result(
    projection: Any, call: Any = None, *,
    managed_store: Any = None, session_id: str = "", run_id: str = "",
    project_path: Any = None, workspace_store: Any = None, ignored_paths: tuple[str, ...] = (),
) -> Any | None:
    """Restore the original observation; unavailable receipts fail closed."""
    from codey.operations.kernel_receipts import restore_result_receipt

    try:
        result = restore_result_receipt(
            projection, store=managed_store, session_id=session_id, run_id=run_id,
            project_path=project_path, workspace_store=workspace_store, ignored_paths=ignored_paths,
        )
        if call is not None and call != result.call:
            return None
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _redelivered_outcome(
    projection: Any,
    rebuilt: Any,
    *,
    turn: int,
    tool_index: int,
    effect_id: str,
) -> RecoveredToolOutcome | None:
    """Convert one rebuilt ToolResult into a redelivery row (no re-execution)."""
    try:
        from codey.toolchain.runtime import ToolOutcome as _Outcome
    except Exception:
        return None
    try:
        st = getattr(projection, "settlement", None)
        ok = str(getattr(st, "status", "") or "") == SETTLEMENT_STATUS_OK
        audit = dict(getattr(rebuilt, "audit", {}) or {})
        exit_code = getattr(st, "exit_code", None)
        exit_code = exit_code if type(exit_code) is int else None
        outcome = _Outcome(
            str(getattr(rebuilt, "model_text", "") or ""),
            ok,
            audit=audit,
            canonical=rebuilt.canonical,
            presentation=rebuilt.presentation,
            exit_code=exit_code,
            truncated=bool(getattr(rebuilt, "truncated", False)),
        )
        from codey.operations.kernel_provenance import _kernel_workspace_identity_of, _trusted_workspace_proof

        identity = _kernel_workspace_identity_of(rebuilt)
        return RecoveredToolOutcome(
            call=rebuilt.call,
            workspace_proof=_trusted_workspace_proof(identity, "in_memory_kernel_result") if identity is not None else None,
            outcome=outcome,
            turn=turn,
            tool_index=tool_index,
            effect_id=effect_id,
            redelivered=True,
        )
    except Exception:
        return None


def _redeliver_settled_batch_for_action(
    action: RuntimeAction,
    projections: tuple[RuntimeEffectProjection, ...],
    *,
    delivery_store: Any,
    mutations: RuntimeMutationLine,
    session_id: str,
    run_id: str,
    project_path: Any = None,
    managed_store: Any = None,
    workspace_store: Any = None,
    ignored_paths: tuple[str, ...] = (),
) -> ResumeRecoveryResult:
    """重发整批已结算结果：逐个重建原结果，不调用执行器。

    任一收据不可重建（缺失/损坏/无 store 可验）即整体 fail-closed，
    绝不回退到重新执行危险工具。
    """
    if action.kind != ACTION_REDELIVER_SETTLED_BATCH or delivery_store is None:
        return ResumeRecoveryResult(ok=False)
    try:
        batches = delivery_store.load_batches(session_id, run_id)
    except Exception:
        return ResumeRecoveryResult(ok=False)
    batch = next(
        (item for item in batches if item.intent.batch_id == action.delivery_batch_id),
        None,
    )
    if batch is None or batch.is_delivered or batch.is_abandoned:
        return ResumeRecoveryResult(ok=False)
    if batch.active_attempts:
        return ResumeRecoveryResult(ok=False)
    recovered_outcomes: list[RecoveredToolOutcome] = []
    recovered_effect_ids: list[str] = []
    read_count = 0
    lookup_count = 0
    for effect_id in tuple(batch.intent.tool_refs):
        projection = _projection_for_effect(projections, effect_id)
        if projection is None or not projection.is_settled:
            return ResumeRecoveryResult(ok=False)
        rebuilt = rebuild_settled_tool_result(
            projection, managed_store=managed_store,
            session_id=session_id, run_id=run_id,
            project_path=project_path, workspace_store=workspace_store, ignored_paths=ignored_paths,
        )
        if rebuilt is None:
            return ResumeRecoveryResult(ok=False)
        row = _redelivered_outcome(
            projection, rebuilt,
            turn=int(projection.intent.turn),
            tool_index=int(projection.intent.tool_index),
            effect_id=effect_id,
        )
        if row is None:
            return ResumeRecoveryResult(ok=False)
        recovered_outcomes.append(row)
        recovered_effect_ids.append(effect_id)
        if row.call.name == "read":
            read_count += 1
        else:
            lookup_count += 1
    recovered_outcomes.sort(key=lambda rec: (rec.turn, rec.tool_index))
    try:
        mutations.record_delivery_recovered(
            session_id,
            run_id,
            batch_id=action.delivery_batch_id,
            recovered_effect_ids=tuple(recovered_effect_ids),
            recovered_reads=read_count,
            recovered_lookups=lookup_count,
        )
    except Exception:
        return ResumeRecoveryResult(ok=False)
    return ResumeRecoveryResult(
        ok=True,
        recovered_tool_outcomes=tuple(recovered_outcomes),
        recovered_tool_result_batch_id=action.delivery_batch_id,
    )


def _replay_safe_tools_for_action(
    mutations: RuntimeMutationLine,
    action: RuntimeAction,
    projections: tuple[RuntimeEffectProjection, ...],
    *,
    delivery_store: Any,
    session_id: str,
    run_id: str,
    project_path: Path | None,
    profile_name: str,
    managed_store: Any = None,
    workspace_store: Any = None,
    ignored_paths: tuple[str, ...] = (),
) -> ResumeRecoveryResult:
    recovered_outcomes: list[RecoveredToolOutcome] = []
    recovered_effect_ids: list[str] = []
    read_count = 0
    lookup_count = 0

    if action.kind == ACTION_REPLAY_SAFE_TOOL_BATCH and delivery_store is not None:
        try:
            batches = delivery_store.load_batches(session_id, run_id)
        except Exception:
            return ResumeRecoveryResult(ok=False)
        batch = next(
            (item for item in batches if item.intent.batch_id == action.delivery_batch_id),
            None,
        )
        if batch is None or not batch.can_recover_before_provider_send:
            return ResumeRecoveryResult(ok=False)
        ordered_effect_ids = tuple(batch.intent.tool_refs)
    else:
        return ResumeRecoveryResult(ok=False)

    for effect_id in ordered_effect_ids:
        projection = _projection_for_effect(projections, effect_id)
        if projection is None:
            return ResumeRecoveryResult(ok=False)
        candidate = candidate_from_intent(projection.intent)
        was_settled = bool(projection.is_settled)
        if was_settled:
            # 已结算原结果优先：直接交付，不重新执行（新读取不得冒充旧结果）。
            # 不再用安全重放候选重建；不可重建则整体失败，不回退重读。
            rebuilt = rebuild_settled_tool_result(
                projection, managed_store=managed_store,
                session_id=session_id, run_id=run_id,
                project_path=project_path, workspace_store=workspace_store, ignored_paths=ignored_paths,
            )
            if rebuilt is None:
                return ResumeRecoveryResult(ok=False)
            # 已结算不再重复 settle（幂等），仅记录交付恢复
            recovered = _redelivered_outcome(
                projection, rebuilt,
                turn=int(projection.intent.turn),
                tool_index=int(projection.intent.tool_index),
                effect_id=effect_id,
            )
            if recovered is None:
                return ResumeRecoveryResult(ok=False)
            recovered_outcomes.append(recovered)
            recovered_effect_ids.append(effect_id)
            if recovered.call.name == "read":
                read_count += 1
            else:
                lookup_count += 1
            continue
        recovered = _try_replay_safe_tool(
            mutations,
            candidate,
            session_id=session_id,
            run_id=run_id,
            project_path=project_path,
            profile_name=profile_name,
            tool_fns=DEFAULT_TOOL_FNS,
            settle=not was_settled,
            managed_store=managed_store,
        )
        if recovered is None:
            return ResumeRecoveryResult(ok=False)
        recovered_outcomes.append(recovered)
        recovered_effect_ids.append(effect_id)
        if recovered.call.name == "read":
            read_count += 1
        else:
            lookup_count += 1

    recovered_outcomes.sort(key=lambda rec: (rec.turn, rec.tool_index))
    if action.delivery_batch_id and recovered_effect_ids:
        try:
            mutations.record_delivery_recovered(
                session_id,
                run_id,
                batch_id=action.delivery_batch_id,
                recovered_effect_ids=tuple(recovered_effect_ids),
                recovered_reads=read_count,
                recovered_lookups=lookup_count,
            )
        except Exception:
            return ResumeRecoveryResult(ok=False)
    return ResumeRecoveryResult(
        ok=True,
        recovered_tool_outcomes=tuple(recovered_outcomes),
        recovered_tool_result_batch_id=action.delivery_batch_id,
    )


def _projection_for_effect(
    projections: tuple[RuntimeEffectProjection, ...],
    effect_id: str,
) -> RuntimeEffectProjection | None:
    return next((item for item in projections if item.intent.effect_id == effect_id), None)


def _settle_interrupted(
    mutations: RuntimeMutationLine,
    *,
    session_id: str,
    run_id: str,
    effect_id: str,
    effect_category: str,
    replay_class: str,
) -> bool:
    sent_state = (
        SENT_STATE_MAYBE_SENT
        if effect_category == EFFECT_CATEGORY_PROVIDER_SEND
        else SENT_STATE_SETTLED
    )
    try:
        mutations.settle_effect(
            session_id,
            run_id,
            RuntimeEffectSettlement(
                effect_id=effect_id,
                effect_category=effect_category,
                session_id=session_id,
                run_id=run_id,
                status="interrupted",
                error_code="interrupted_by_crash",
                sent_state=sent_state,
                replay_class=replay_class,
            ),
        )
    except Exception:
        return False
    return True


def delivered_from_frame(frame: Any, *, effect_scope: str = "") -> dict[str, Any]:
    """Rebuild durable delivery map from recovered frame rows.

    Identity is run+turn+index, never tool name+args. Every row goes through
    the single ``build_recovered_result`` protocol with strict turn/index
    (exact non-negative ints, no bool, no duplicates). Settled redelivery
    rows (``redelivered=True``) rebuild the original receipt for any tool;
    safe-replay rows stay safe-tools-only and carry no trusted provenance.
    Malformed, duplicate, or unsafe replay rows raise ``RecoveryFailed``.
    """
    from codey.operations.kernel_errors import RecoveryFailed

    try:
        from codey.operations.kernel_recovery_result import (
            _strict_slot_index,
            build_recovered_result,
            spec_for_recovered_row,
        )
        from codey.operations.task_session import turn_effect_id
    except Exception as exc:
        raise RecoveryFailed(f"recovery rebuild unavailable: {exc}") from exc
    delivered: dict[str, Any] = {}
    seen: set[tuple[int, int]] = set()
    for item in getattr(frame, "recovered_tool_outcomes", ()) or ():
        try:
            try:
                turn = _strict_slot_index(getattr(item, "turn", None), field="turn")
                index = _strict_slot_index(getattr(item, "tool_index", None), field="tool_index")
            except RecoveryFailed:
                raise
            except Exception as exc:
                raise RecoveryFailed(f"malformed turn/index: {exc}") from exc
            if (turn, index) in seen:
                raise RecoveryFailed(f"duplicate recovered slot: turn={turn} index={index}")
            seen.add((turn, index))
            spec = spec_for_recovered_row(item)
            rebuilt = build_recovered_result(spec)
            identity_ref = f"{frame.run_id}:{effect_scope}" if effect_scope else frame.run_id
            identity = turn_effect_id(identity_ref, turn, index)
            if identity in delivered:
                raise RecoveryFailed(f"duplicate recovered slot: turn={turn} index={index}")
            delivered[identity] = rebuilt
        except RecoveryFailed:
            raise
        except Exception as exc:
            effect = str(getattr(item, "effect_id", "") or "")
            raise RecoveryFailed(
                f"recovered row unreadable (effect={effect or '?'} turn={getattr(item, 'turn', '?')} "
                f"index={getattr(item, 'tool_index', '?')}): {exc}"
            ) from exc
    return delivered


def record_entry_policy(
    mutations: Any,
    *,
    session_id: str,
    run_id: str,
    policy: Any,
) -> None:
    """Record the entry authorization snapshot in the existing session log.

    The session log stays the single durable fact source; no competing
    persistent log is created. Persistence failures stop durable tasks.
    """
    if mutations is None or policy is None:
        return
    try:
        payload = policy.to_payload() if hasattr(policy, "to_payload") else dict[str, object]()
    except Exception as exc:
        raise RuntimeError(f"task policy serialization failed: {exc}") from exc
    setter = getattr(mutations, "set_task_policy", None)
    if not callable(setter):
        # Lightweight callers without the durable runtime are not a recovery
        # boundary. The real RuntimeMutationLine always exposes this setter;
        # when it does, failures propagate to prevent an unrecorded policy.
        if not hasattr(mutations, "session_log"):
            return
        raise RuntimeError("task policy persistence is unavailable")
    setter(session_id, run_id, policy=payload)


def rebuilt_policy_from_log(
    session_log: Any, *, session_id: str, run_id: str,
) -> Any:
    """Restore original grants AND completion requirements, or stop recovery."""
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.operation_state import operation_state_from_entries

    try:
        if session_log is None:
            raise ValueError("policy log is unavailable")
        state = operation_state_from_entries(
            session_log.entries(session_id), session_id=session_id, run_id=run_id,
        )
        payload = state.task_policy if state is not None else None
        if not isinstance(payload, dict) or not payload:
            raise ValueError("original task policy is missing")
        return TaskPolicy.from_payload(payload)
    except Exception as exc:
        raise RecoveryFailed(f"task policy recovery failed: {exc}") from exc


__all__ = [
    "ResumeRecoveryResult",
    "delivered_from_frame",
    "rebuild_settled_tool_result",
    "rebuilt_policy_from_log",
    "record_entry_policy",
    "recover_effects_for_resume",
]
