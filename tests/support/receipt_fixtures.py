"""Test-only adapters for historical receipt fixtures; never execute actions.

Runtime execution and restoration use KernelContext and the sole driver.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from codey.agents.request import RecoveredToolOutcome
from codey.operations.recovery import durable_task_for
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_TOOL_CALL,
    SETTLEMENT_STATUS_OK,
    RuntimeEffectProjection,
    RuntimeEffectStore,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReceiptFacts:
    ok: bool
    recovered_tool_outcomes: tuple[RecoveredToolOutcome, ...] = ()
    recovered_tool_result_batch_id: str = ""
    settled_tool_outcomes: tuple[RecoveredToolOutcome, ...] = ()


def inspect_receipt_facts(
    deps: Any,
    *,
    session_id: str,
    run_id: str,
    project: str,
    task_kind: str,
    ignored_paths: tuple[str, ...] = (),
) -> ReceiptFacts:
    """Recover pending delivery and project all settled facts independently.

    A delivered batch stops needing provider messages, but its observations
    remain part of the task on every restart. No executor runs while loading
    settled facts, and an unavailable receipt never becomes an empty task.
    """
    durable = durable_task_for(
        getattr(getattr(deps, "state", None), "durable", None), run_id
    )
    if durable is None:
        return ReceiptFacts(ok=False)
    from codey.durable.checkpoint import checkpoint_entries
    from codey.runtime.core.operation_reducer import ACTION_FAIL_INVARIANT, next_runtime_action
    from codey.runtime.effects.effect_records import RuntimeEffectError
    from codey.runtime.log.session_view import load_session_view

    # Loading restores observations only. The operation driver owns pending
    # execution, progress reduction, generation boundaries and redelivery.
    # In particular, never run a tool from this entry pre-pass.
    pending = ReceiptFacts(ok=True)
    # Durable effect records are sufficient to reconstruct settled receipts.
    # A runtime JSONL projection may be absent after a clean cold start or
    # deliberately disabled in recovery tests; that must not erase facts.
    store = _runtime_effect_store(deps)
    if store is None:
        store = RuntimeEffectStore(durable=durable)
    try:
        task, effects = durable.task_facts(run_id)
        view = load_session_view(checkpoint_entries(task, effects, session_id=session_id),
                                 session_id=session_id, run_id=run_id)
        if next_runtime_action(view).kind == ACTION_FAIL_INVARIANT:
            return ReceiptFacts(ok=False)
        facts = _settled_facts(
            store.load_effects(session_id, run_id), pending.recovered_tool_outcomes,
            deps=deps, session_id=session_id, run_id=run_id, project=project,
            ignored_paths=ignored_paths,
            active_batch_id=pending.recovered_tool_result_batch_id,
        )
    except (OSError, ValueError, TypeError, AttributeError, RuntimeEffectError):
        return ReceiptFacts(ok=False)
    batches = [batch for batch in view.batches if not batch.is_terminal and not batch.send_attempts]
    batch_id = batches[0].intent.batch_id if len(batches) == 1 else ""
    return replace(pending, settled_tool_outcomes=facts, recovered_tool_result_batch_id=batch_id)


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


def _runtime_effect_store(deps: Any) -> Any:
    return getattr(deps, "runtime_effects", None)


def _managed_output_store(deps: Any) -> Any:
    store = getattr(deps, "managed_outputs", None)
    if store is not None:
        return store
    state = getattr(deps, "state", None)
    return getattr(state, "managed_outputs", None)


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
