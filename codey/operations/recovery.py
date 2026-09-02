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
from codey.runtime.safe_tool_replay import SafeToolReplayCandidate, candidate_from_effect


@dataclass(frozen=True)
class ResumeRecoveryResult:
    ok: bool
    recovered_tool_outcomes: tuple[RecoveredToolOutcome, ...] = ()


def recover_effects_for_resume(
    deps: Any,
    *,
    session_id: str,
    run_id: str,
    project: str,
    task_kind: str,
) -> ResumeRecoveryResult:
    effects_store = _runtime_effect_store(deps)
    if effects_store is None:
        return ResumeRecoveryResult(ok=True)
    try:
        pending = effects_store.pending_effects(session_id, run_id)
    except Exception:
        return ResumeRecoveryResult(ok=False)

    project_path = _writer_project_path(project, task_kind)
    profile_name = profile_for_task_kind(task_kind, phase="writer").name if project_path is not None else ""
    recovered_outcomes: list[RecoveredToolOutcome] = []

    for projection in pending:
        intent = projection.intent
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

    return ResumeRecoveryResult(ok=True, recovered_tool_outcomes=tuple(recovered_outcomes))


def _runtime_effect_store(deps: Any) -> Any:
    store = getattr(deps, "runtime_effects", None)
    if store is not None:
        return store
    state = getattr(deps, "state", None)
    return getattr(state, "runtime_effects", None)


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
