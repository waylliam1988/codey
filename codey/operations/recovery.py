"""Resume recovery for pending runtime effects."""

from __future__ import annotations

import logging
from collections.abc import Mapping
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
from codey.durable import decide_recovery
from codey.durable.model import EffectRecord, thaw_json
from codey.durable.session import DurableSession
from codey.policies.permissions import profile_for_task_kind
from codey.runtime.core.operation_payload import RuntimeOperationTransitionError
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_TOOL_CALL,
    SETTLEMENT_STATUS_ERROR,
    SETTLEMENT_STATUS_OK,
    RuntimeEffectIntent,
    RuntimeEffectProjection,
    RuntimeEffectSettlement,
    RuntimeEffectStore,
)
from codey.runtime.effects.replay_policy import ReplayClass
from codey.runtime.effects.safe_tool_replay import candidate_from_intent
from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
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
    durable = durable_task_for(
        getattr(getattr(deps, "state", None), "durable", None), run_id
    )
    if durable is None:
        return ResumeRecoveryResult(ok=False)
    pending = _recover_pending_effects(
        deps, session_id=session_id, run_id=run_id, project=project,
        task_kind=task_kind, ignored_paths=ignored_paths,
        durable=durable,
    )
    if not pending.ok:
        return pending
    # Durable effect records are sufficient to reconstruct settled receipts.
    # A runtime JSONL projection may be absent after a clean cold start or
    # deliberately disabled in recovery tests; that must not erase facts.
    store = _runtime_effect_store(deps)
    if store is None:
        store = RuntimeEffectStore(durable=durable)
    try:
        facts = _settled_facts(
            store.load_effects(session_id, run_id), pending.recovered_tool_outcomes,
            deps=deps, session_id=session_id, run_id=run_id, project=project,
            ignored_paths=ignored_paths,
            active_batch_id=pending.recovered_tool_result_batch_id,
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return ResumeRecoveryResult(ok=False)
    return replace(pending, settled_tool_outcomes=facts)


def _settled_facts(
    projections: tuple[RuntimeEffectProjection, ...], pending_rows: tuple[RecoveredToolOutcome, ...], *,
    deps: Any, session_id: str, run_id: str, project: str, ignored_paths: tuple[str, ...],
    active_batch_id: str = "",
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
    durable: DurableSession,
) -> ResumeRecoveryResult:
    mutations = _runtime_mutations(deps)
    durable_decision, terminal = _apply_durable_recovery(
        durable, run_id, mutations=mutations, session_id=session_id,
    )
    if terminal is not None:
        return terminal
    if durable_decision.action == "recover_tool_effects":
        return _recover_durable_tool_effects(
            durable, run_id=run_id, project=project,
            task_kind=task_kind, deps=deps, mutations=mutations,
        )
    if durable_decision.action in {"continue", "settle_provider_unknown", "settle_tool_unknown"}:
        recovered_batch_id = ""
        if durable_decision.action == "continue":
            recovered_batch_id = _recover_settled_delivery(
                durable, mutations=mutations, session_id=session_id, run_id=run_id,
            )
        return ResumeRecoveryResult(
            ok=True,
            recovered_tool_result_batch_id=recovered_batch_id,
        )
    return ResumeRecoveryResult(ok=False)


def _settled_delivery_batch_id(
    mutations: RuntimeMutationLine | None,
    *,
    session_id: str,
    run_id: str,
) -> str:
    """Return a projection batch only after durable recovery says continue."""
    if mutations is None:
        return ""
    delivery_store = ToolResultDeliveryStore(durable=mutations.durable)
    try:
        batches = delivery_store.load_batches(session_id, run_id)
    except (OSError, ValueError, TypeError, AttributeError):
        return ""
    for batch in reversed(batches):
        if batch.is_delivered:
            continue
        if any(item.ref and not item.is_denied for item in batch.intent.items):
            return batch.intent.batch_id
    return ""


def _recover_settled_delivery(
    durable: DurableSession,
    *,
    mutations: RuntimeMutationLine | None,
    session_id: str,
    run_id: str,
) -> str:
    """Mark a committed tool receipt as recoverable for the next provider send.

    The receipt and task checkpoint are canonical durable facts.  The
    recovered marker is only a derived delivery projection, so it is written
    after reading those facts and is safe to repeat across restarts.
    """
    if mutations is None:
        return ""
    try:
        records = durable.effects(run_id)
        settled = tuple(
            record for record in records
            if record.category == EFFECT_CATEGORY_TOOL_CALL
            and record.settlement is not None
        )
        by_batch: dict[str, list[EffectRecord]] = {}
        for record in settled:
            batch_id = _effect_batch_id(record)
            if batch_id:
                by_batch.setdefault(batch_id, []).append(record)
        for batch_id, rows in by_batch.items():
            mutations.record_delivery_recovered(
                session_id,
                run_id,
                batch_id=batch_id,
                recovered_effect_ids=tuple(row.effect_id for row in rows),
                recovered_reads=sum(
                    str(_intent_object(row).get("tool_name") or "") == "read" for row in rows
                ),
                recovered_lookups=sum(
                    str(_intent_object(row).get("tool_name") or "") != "read" for row in rows
                ),
            )
            return batch_id
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeOperationTransitionError):
        return ""
    return ""


def _recover_durable_tool_effects(
    durable: DurableSession,
    *,
    run_id: str,
    project: str,
    task_kind: str,
    deps: Any,
    mutations: RuntimeMutationLine | None = None,
) -> ResumeRecoveryResult:
    """Recover safe information tools from canonical durable intents alone."""
    outcomes: list[RecoveredToolOutcome] = []
    try:
        records = durable.effects(run_id)
        pending_records = tuple(
            record for record in records
            if record.status == "pending" and record.category == EFFECT_CATEGORY_TOOL_CALL
        )
        if not pending_records:
            return ResumeRecoveryResult(ok=True)
        # A safe effect is not independently replayable when it belonged to a
        # batch that also contained an unsafe/provider effect. The batch ID is
        # part of the durable intent, so this decision does not depend on the
        # JSONL projection being complete.
        all_effects = tuple(records)
        for record in pending_records:
            batch_id = _effect_batch_id(record)
            if not isinstance(batch_id, str) or not batch_id:
                continue
            siblings = tuple(sibling for sibling in all_effects if _effect_batch_id(sibling) == batch_id)
            if any(
                sibling.category != EFFECT_CATEGORY_TOOL_CALL
                or sibling.replay_class != ReplayClass.SAFE
                for sibling in siblings
            ):
                return _fail_mixed_tool_batch(
                    durable, pending_records, batch_id, mutations=mutations,
                )
        project_path = _writer_project_path(project, task_kind)
        if project_path is None:
            for record in pending_records:
                effect_session = str(_intent_object(record).get("session_id") or "")
                _settle_unknown_and_fail(
                    durable, run_id, record.effect_id, mutations=mutations,
                    session_id=effect_session,
                    reason="tool effect cannot be replayed for this task kind",
                )
            return ResumeRecoveryResult(ok=False)
        profile_name = profile_for_task_kind(task_kind, phase="writer").name
        intents: list[RuntimeEffectIntent] = []
        for record in pending_records:
            intents.append(RuntimeEffectIntent.from_payload(_intent_object(record)))
        batch_id = ""
        if mutations is not None:
            batch_id = _pending_delivery_batch(
                mutations, run_id=run_id, intents=tuple(intents),
            )
        for record, intent in zip(pending_records, intents, strict=True):
            candidate = candidate_from_intent(intent)
            if candidate is None:
                _settle_unknown_and_fail(
                    durable, run_id, record.effect_id, mutations=mutations,
                    session_id=str(intent.session_id),
                    reason="tool effect replay arguments were invalid",
                )
                return ResumeRecoveryResult(ok=False)
            decision, replay = evaluate_tool_call_policy_for(
                candidate.call,
                project=project_path,
                permission_profile=profile_name,
                approval_available=False,
                phase="writer",
            )
            if policy_denied(decision) or replay.replay_class != ReplayClass.SAFE:
                _settle_unknown_and_fail(
                    durable, run_id, record.effect_id, mutations=mutations,
                    session_id=str(intent.session_id),
                    reason="tool effect was not safe to replay",
                )
                return ResumeRecoveryResult(ok=False)
            outcome = execute_information_tool_call(project_path, DEFAULT_TOOL_FNS, candidate.call)
            _settle_durable_tool_observation(
                durable, record, outcome, candidate.call,
                managed_store=_managed_output_store(deps), mutations=mutations,
            )
            outcomes.append(RecoveredToolOutcome(
                call=candidate.call,
                outcome=outcome,
                turn=candidate.turn,
                tool_index=candidate.tool_index,
                effect_id=candidate.effect_id,
            ))
        if mutations is not None and outcomes:
            mutations.record_delivery_recovered(
                str(intents[0].session_id), run_id,
                batch_id=batch_id,
                recovered_effect_ids=tuple(row.effect_id for row in outcomes),
                recovered_reads=sum(row.call.name == "read" for row in outcomes),
                recovered_lookups=sum(row.call.name != "read" for row in outcomes),
            )
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return ResumeRecoveryResult(ok=False)
    return ResumeRecoveryResult(
        ok=True,
        recovered_tool_outcomes=tuple(outcomes),
        recovered_tool_result_batch_id=batch_id,
    )


def _intent_object(record: EffectRecord) -> dict[str, object]:
    raw = thaw_json(record.intent)
    if not isinstance(raw, dict):
        raise ValueError("durable effect intent must be an object")
    return raw


def _effect_batch_id(record: EffectRecord) -> str | None:
    value = _intent_object(record).get("delivery_batch_id")
    return value if isinstance(value, str) and value else None


def _fail_mixed_tool_batch(
    durable: DurableSession,
    records: tuple[EffectRecord, ...],
    batch_id: str,
    *,
    mutations: RuntimeMutationLine | None,
) -> ResumeRecoveryResult:
    for record in records:
        if _effect_batch_id(record) != batch_id:
            continue
        intent = _intent_object(record)
        _settle_unknown_and_fail(
            durable, record.task_id, record.effect_id, mutations=mutations,
            session_id=str(intent.get("session_id") or ""),
            reason="mixed effect batch cannot be replayed safely",
        )
    return ResumeRecoveryResult(ok=False)


def _pending_delivery_batch(
    mutations: RuntimeMutationLine,
    *,
    run_id: str,
    intents: tuple[RuntimeEffectIntent, ...],
) -> str:
    """Load the committed batch; recovery never repairs a second fact source."""
    if mutations.durable is None:
        raise ValueError("production recovery requires durable runtime mutations")
    session_id = str(intents[0].session_id)
    pending_ids = {intent.effect_id for intent in intents}
    batches = ToolResultDeliveryStore(durable=mutations.durable).load_batches(session_id, run_id)
    matching = [batch for batch in batches
                if not batch.is_terminal and pending_ids.issubset(set(batch.intent.tool_refs))]
    if len(matching) != 1:
        raise ValueError("durable checkpoint has no unique pending delivery batch")
    return matching[0].intent.batch_id


def _settle_durable_tool_observation(
    durable: DurableSession,
    record: EffectRecord,
    outcome: Any,
    call: Any,
    *,
    managed_store: Any,
    mutations: RuntimeMutationLine | None,
) -> RuntimeEffectSettlement:
    from codey.operations.kernel_receipts import result_receipt_fields, tool_history_messages
    from codey.runtime.core.models import ToolResult

    result = ToolResult(
        ok=bool(outcome.ok),
        call=call,
        model_text=str(outcome.model_text or ""),
        audit=dict(outcome.audit or {}),
        canonical=dict(outcome.canonical) if isinstance(outcome.canonical, Mapping) else {},
        presentation=dict(outcome.presentation) if isinstance(outcome.presentation, Mapping) else {},
        truncated=bool(outcome.truncated),
    )
    status = SETTLEMENT_STATUS_OK if result.ok else SETTLEMENT_STATUS_ERROR
    raw_intent = thaw_json(record.intent)
    if not isinstance(raw_intent, dict):
        raise ValueError("durable tool intent must be an object")
    settlement = RuntimeEffectSettlement(
        effect_id=record.effect_id,
        effect_category=record.category,
        session_id=str(raw_intent.get("session_id") or ""),
        run_id=str(raw_intent.get("run_id") or record.task_id),
        status=status,
        lane=str(raw_intent.get("lane") or ""),
        operation_id=str(raw_intent.get("operation_id") or ""),
        error_code="" if result.ok else str(outcome.error_code or "tool_error"),
        replay_class=record.replay_class,
        replay_count=1,
        replayed_from_effect_id=record.effect_id,
        **result_receipt_fields(
            result, store=managed_store,
            session_id=str(raw_intent.get("session_id") or ""),
            run_id=str(raw_intent.get("run_id") or record.task_id),
            effect_id=record.effect_id,
        ),
    )
    if mutations is not None:
        mutations.settle_tool_effect(settlement.session_id, record.task_id, settlement,
                                    messages=tool_history_messages(settlement))
    else:
        durable.settle_effect(
            record.task_id,
            EffectRecord(
                record.effect_id, record.task_id, record.category,
                record.replay_class, record.args_digest, status,
                record.intent, settlement.to_payload(),
            ),
        )
    return settlement


def _apply_durable_recovery(
    durable: Any,
    task_id: str,
    *,
    mutations: RuntimeMutationLine | None,
    session_id: str,
) -> tuple[Any, ResumeRecoveryResult | None]:
    """Apply recovery decisions that need no runtime projection.

    This boundary is intentionally small: durable facts decide whether a
    task may continue, while the legacy runtime projection can only provide
    receipts for replay/redelivery after that decision.
    """
    try:
        decision = decide_recovery(durable.task(task_id), durable.effects(task_id))
    except (KeyError, ValueError, TypeError):
        return None, ResumeRecoveryResult(ok=False)
    if decision.action == "fail_invariant":
        try:
            task = durable.task(task_id)
            pending = tuple(effect for effect in durable.effects(task_id) if effect.settlement is None)
            for effect in pending:
                raw_intent = thaw_json(effect.intent)
                _settle_unknown_effect(
                    durable, task_id, effect.effect_id, mutations=mutations,
                    session_id=(
                        str(raw_intent.get("session_id") or session_id)
                        if isinstance(raw_intent, dict)
                        else session_id
                    ),
                )
            # A provider can already have a durable ``maybe_sent`` settlement;
            # there is then nothing left to settle, but the unknown checkpoint
            # still has to be sealed so restart cannot quietly continue it.
            if task.phase not in {"completed", "failed", "cancelled"}:
                checkpoint = durable.task(task_id)
                durable.fail_task(
                    task_id,
                    f"durable recovery invariant failed: {decision.reason}",
                    state=dict(checkpoint.state),
                )
        except Exception:
            return decision, ResumeRecoveryResult(ok=False)
        return decision, ResumeRecoveryResult(ok=False)
    if decision.action not in {"settle_provider_unknown", "settle_tool_unknown"}:
        return decision, None
    try:
        for effect_id in decision.effect_ids:
            _settle_unknown_effect(
                durable, task_id, effect_id,
                mutations=mutations, session_id=session_id,
            )
        checkpoint = durable.task(task_id)
        durable.fail_task(
            task_id,
            (
                "provider response was unknown after process restart; request was not replayed"
                if decision.action == "settle_provider_unknown"
                else "tool effect outcome was unknown after process restart; effect was not replayed"
            ),
            state=dict(checkpoint.state),
        )
    except Exception:
        return decision, ResumeRecoveryResult(ok=False)
    return decision, ResumeRecoveryResult(ok=False)


def _settle_unknown_and_fail(
    durable: DurableSession,
    task_id: str,
    effect_id: str,
    *,
    mutations: RuntimeMutationLine | None,
    session_id: str,
    reason: str,
) -> None:
    _settle_unknown_effect(
        durable, task_id, effect_id, mutations=mutations, session_id=session_id,
    )
    checkpoint = durable.task(task_id)
    durable.fail_task(task_id, reason, state=dict(checkpoint.state))


def _settle_unknown_effect(
    durable: DurableSession,
    task_id: str,
    effect_id: str,
    *,
    mutations: RuntimeMutationLine | None,
    session_id: str,
) -> None:
    task, effects = durable.task_facts(task_id)
    effect = next(item for item in effects if item.effect_id == effect_id)
    # Shell approval claims are durable external effects, but they are not a
    # runtime provider/tool projection. Their unknown outcome must still be
    # sealed in the canonical effect table without asking the JSONL reducer to
    # interpret a category it does not own.
    if mutations is None or effect.category == "shell":
        durable.settle_unknown_effect(task_id, effect_id)
        return
    from codey.durable.effects import interrupted_effect
    settlement = interrupted_effect(task, effect).settlement
    raw = thaw_json(settlement)
    if not isinstance(raw, dict):
        raise ValueError("interrupted effect settlement must be an object")
    mutations.settle_effect(session_id, task_id, RuntimeEffectSettlement.from_payload(raw))


def _runtime_mutations(deps: Any) -> RuntimeMutationLine | None:
    mutations = getattr(deps, "runtime_mutations", None)
    durable = getattr(getattr(deps, "state", None), "durable", None)
    if isinstance(mutations, RuntimeMutationLine) and mutations.durable is durable and durable is not None:
        return mutations
    return None


def durable_task_for(value: Any, task_id: str) -> DurableSession | None:
    """Return durable authority only when this run was actually admitted."""
    if not isinstance(value, DurableSession) or not task_id:
        return None
    try:
        value.task(task_id)
    except (KeyError, ValueError, TypeError):
        return None
    return value


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
    durable: DurableSession | None = None,
) -> None:
    """Record immutable authorization with the operation checkpoint."""
    if policy is None:
        return
    try:
        payload = policy.to_payload() if hasattr(policy, "to_payload") else dict[str, object]()
    except Exception as exc:
        raise RuntimeError(f"task policy serialization failed: {exc}") from exc
    if isinstance(mutations, RuntimeMutationLine) and durable is not None and mutations.durable is durable:
        mutations.set_task_policy(session_id, run_id, policy=payload)
        return
    if durable is not None:
        durable.record_task_policy(run_id, payload)
    if mutations is None:
        return
    setter = getattr(mutations, "set_task_policy", None)
    if not callable(setter):
        # Lightweight callers without the durable runtime are not a recovery
        # boundary. The real RuntimeMutationLine always exposes this setter;
        # when it does, failures propagate to prevent an unrecorded policy.
        raise RuntimeError("task policy persistence is unavailable")
    setter(session_id, run_id, policy=payload)


def policy_from_durable_task(
    durable: DurableSession | None, *, task_id: str,
) -> Any:
    """Restore the immutable authorization snapshot from one durable task."""
    from codey.operations.kernel_errors import RecoveryFailed
    from codey.policies.task_policy import TaskPolicy

    if durable is None or not task_id:
        raise RecoveryFailed("durable task policy recovery requires an admitted task")
    try:
        task = durable.task(task_id)
        state = thaw_json(task.state)
        payload = state.get("task_policy") if isinstance(state, dict) else None
        if not isinstance(payload, dict) or not payload:
            raise ValueError("durable task authorization snapshot is missing")
        return TaskPolicy.from_payload(payload)
    except Exception as exc:
        raise RecoveryFailed(f"durable task policy recovery failed: {exc}") from exc


__all__ = [
    "ResumeRecoveryResult",
    "delivered_from_frame",
    "rebuild_settled_tool_result",
    "policy_from_durable_task",
    "record_entry_policy",
    "recover_effects_for_resume",
    "durable_task_for",
]
