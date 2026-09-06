"""Resume recovery for pending runtime effects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codey.agents.request import RecoveredToolOutcome
from codey.agents.tools import DEFAULT_TOOL_FNS
from codey.agents.tool_execution import (
    evaluate_tool_call_policy_for,
    execute_information_tool_call,
    policy_denied,
)
from codey.policies.permissions import profile_for_task_kind
from codey.runtime import cancellation
from codey.runtime.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    RuntimeEffectProjection,
    RuntimeEffectSettlement,
    SENT_STATE_MAYBE_SENT,
    SENT_STATE_SETTLED,
    SETTLEMENT_STATUS_ERROR,
    SETTLEMENT_STATUS_OK,
)
from codey.runtime.drive import peek_next_action
from codey.runtime.mutation_line import RuntimeMutationLine
from codey.runtime.operation_reducer import (
    ACTION_CONTINUE,
    ACTION_FAIL_INVARIANT,
    ACTION_REPLAY_SAFE_TOOL_BATCH,
    ACTION_SETTLE_PROVIDER_UNKNOWN,
    ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS,
    ACTION_TERMINAL,
    RuntimeAction,
)
from codey.runtime.replay_policy import ReplayClass
from codey.runtime.safe_tool_replay import (
    SafeToolReplayCandidate,
    candidate_from_intent,
)


@dataclass(frozen=True)
class ResumeRecoveryResult:
    ok: bool
    recovered_tool_outcomes: tuple[RecoveredToolOutcome, ...] = ()
    recovered_tool_result_batch_id: str = ""


def recover_effects_for_resume(
    deps: Any,
    *,
    session_id: str,
    run_id: str,
    project: str,
    task_kind: str,
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
    mutations = getattr(deps, "runtime_mutations", None)
    if mutations is not None:
        return mutations
    state = getattr(deps, "state", None)
    return getattr(state, "runtime_mutations", None)


def _runtime_effect_store(deps: Any) -> Any:
    store = getattr(deps, "runtime_effects", None)
    if store is not None:
        return store
    state = getattr(deps, "state", None)
    return getattr(state, "runtime_effects", None)


def _tool_result_delivery_store(deps: Any) -> Any:
    store = getattr(deps, "tool_result_delivery", None)
    if store is not None:
        return store
    state = getattr(deps, "state", None)
    return getattr(state, "tool_result_delivery", None)


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
) -> RecoveredToolOutcome | None:
    if candidate is None or project_path is None:
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
            return None
        outcome = execute_information_tool_call(project_path, tool_fns, candidate.call)
        status = SETTLEMENT_STATUS_OK if outcome.ok else SETTLEMENT_STATUS_ERROR
        error_code = str(outcome.error_code or ("" if outcome.ok else "error"))[:80]
        if settle:
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
    except Exception:
        return None


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
        recovered = _try_replay_safe_tool(
            mutations,
            candidate,
            session_id=session_id,
            run_id=run_id,
            project_path=project_path,
            profile_name=profile_name,
            tool_fns=DEFAULT_TOOL_FNS,
            settle=projection.is_pending,
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


__all__ = [
    "ResumeRecoveryResult",
    "recover_effects_for_resume",
]
