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
    RuntimeEffectSettlement,
    SENT_STATE_MAYBE_SENT,
    SENT_STATE_SETTLED,
    SETTLEMENT_STATUS_ERROR,
    SETTLEMENT_STATUS_OK,
)
from codey.runtime.replay_policy import ReplayClass
from codey.runtime.safe_tool_replay import (
    SafeToolReplayCandidate,
    candidate_from_effect,
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
    if effects_store is None:
        return ResumeRecoveryResult(ok=True)
    try:
        pending = effects_store.pending_effects(session_id, run_id)
        all_projections = effects_store.load_effects(session_id, run_id)
    except Exception:
        return ResumeRecoveryResult(ok=False)

    project_path = _writer_project_path(project, task_kind)
    profile_name = profile_for_task_kind(task_kind, phase="writer").name if project_path is not None else ""
    recovered_outcomes: list[RecoveredToolOutcome] = []
    consumed_effect_ids: set[str] = set()
    recovered_batch_id = ""

    blocked_effect_ids: set[str] = set()

    # 1. Process undelivered all-safe batches if delivery store is available
    if delivery_store is not None and project_path is not None:
        try:
            raw_batches = delivery_store.load_batches(session_id, run_id)
            all_batches = tuple(raw_batches) if isinstance(raw_batches, (list, tuple)) else ()
        except Exception:
            return ResumeRecoveryResult(ok=False)

        effect_by_id = {
            p.intent.effect_id: p
            for p in all_projections
            if hasattr(p, "intent") and hasattr(p.intent, "effect_id")
        }

        recovered_a_batch = False
        for batch in all_batches:
            if not batch.can_recover_before_provider_send or recovered_a_batch:
                # Any batch that cannot be recovered whole blocks its effects from single-effect fallback
                blocked_effect_ids.update(batch.intent.tool_refs)
                continue

            batch_candidates: list[tuple[SafeToolReplayCandidate, bool]] = []
            batch_valid = True

            # Match effects referenced in the batch
            for ref in batch.intent.tool_refs:
                proj = effect_by_id.get(ref)
                if proj is None:
                    # Missing effect for batch ref -> fail closed
                    batch_valid = False
                    break
                candidate = candidate_from_intent(proj.intent)
                if candidate is None:
                    batch_valid = False
                    break
                # Validate policy
                policy_decision, replay_decision = evaluate_tool_call_policy_for(
                    candidate.call,
                    project=project_path,
                    permission_profile=profile_name,
                    approval_available=False,
                    phase="writer",
                )
                if policy_denied(policy_decision) or replay_decision.replay_class != ReplayClass.SAFE:
                    batch_valid = False
                    break
                batch_candidates.append((candidate, proj.is_pending))

            if not batch_valid or not batch_candidates:
                blocked_effect_ids.update(batch.intent.tool_refs)
                continue

            batch_outcomes: list[RecoveredToolOutcome] = []
            batch_effect_ids: list[str] = []
            read_count = 0
            lookup_count = 0

            for candidate, is_pending in batch_candidates:
                outcome = execute_information_tool_call(project_path, DEFAULT_TOOL_FNS, candidate.call)
                if candidate.call.name == "read":
                    read_count += 1
                else:
                    lookup_count += 1

                if is_pending:
                    status = SETTLEMENT_STATUS_OK if outcome.ok else SETTLEMENT_STATUS_ERROR
                    error_code = str(outcome.error_code or ("" if outcome.ok else "error"))[:80]
                    effects_store.record_settlement(
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

                batch_recovered = RecoveredToolOutcome(
                    call=candidate.call,
                    outcome=outcome,
                    turn=candidate.turn,
                    tool_index=candidate.tool_index,
                    effect_id=candidate.effect_id,
                )
                batch_outcomes.append(batch_recovered)
                batch_effect_ids.append(candidate.effect_id)
                consumed_effect_ids.add(candidate.effect_id)

            if batch_outcomes:
                delivery_store.record_recovered(
                    session_id,
                    run_id,
                    batch_id=batch.intent.batch_id,
                    recovered_effect_ids=tuple(batch_effect_ids),
                    recovered_reads=read_count,
                    recovered_lookups=lookup_count,
                )
                recovered_outcomes.extend(batch_outcomes)
                recovered_batch_id = batch.intent.batch_id
                recovered_a_batch = True

    # 2. Process remaining pending effects
    for projection in pending:
        intent = projection.intent
        if intent.effect_id in consumed_effect_ids:
            continue

        if intent.effect_id not in blocked_effect_ids:
            candidate = candidate_from_effect(projection)
            recovered = _try_replay_safe_tool(
                effects_store,
                candidate,
                session_id=session_id,
                run_id=run_id,
                project_path=project_path,
                profile_name=profile_name,
                tool_fns=DEFAULT_TOOL_FNS,
            )
            if recovered is not None:
                recovered_outcomes.append(recovered)
                consumed_effect_ids.add(intent.effect_id)
                continue

        if not _settle_interrupted(
            effects_store,
            session_id=session_id,
            run_id=run_id,
            effect_id=intent.effect_id,
            effect_category=intent.effect_category,
            replay_class=intent.replay_class,
        ):
            return ResumeRecoveryResult(ok=False)

    recovered_outcomes.sort(key=lambda rec: (rec.turn, rec.tool_index))
    return ResumeRecoveryResult(
        ok=True,
        recovered_tool_outcomes=tuple(recovered_outcomes),
        recovered_tool_result_batch_id=recovered_batch_id,
    )


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
    effects_store: Any,
    candidate: SafeToolReplayCandidate | None,
    *,
    session_id: str,
    run_id: str,
    project_path: Path | None,
    profile_name: str,
    tool_fns: Any,
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
        effects_store.record_settlement(
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


def _settle_interrupted(
    effects_store: Any,
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
        effects_store.synthesize_interrupted(
            session_id=session_id,
            run_id=run_id,
            effect_id=effect_id,
            reason="interrupted_by_crash",
            replay_class=replay_class,
            sent_state=sent_state,
        )
    except Exception:
        return False
    return True


__all__ = [
    "ResumeRecoveryResult",
    "recover_effects_for_resume",
]
