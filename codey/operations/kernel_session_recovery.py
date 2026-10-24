"""Restore shared task facts and pending delivery from durable outcomes."""
from __future__ import annotations

from typing import Any

from codey.operations.kernel_errors import RecoveryFailed
from codey.operations.kernel_facts import project_result_observation
from codey.operations.kernel_recovery_result import (
    _strict_slot_index,
    build_recovered_result,
    frame_outcome_exit_code,
    frame_outcome_ok,
    spec_for_recovered_row,
)
from codey.operations.recovery import delivered_from_frame
from codey.operations.task_session import turn_effect_id


def _validate_recovered_rows(raw_rows: list[Any]) -> None:
    seen: set[tuple[int, int]] = set()
    for row in raw_rows:
        try:
            if getattr(row, "call", None) is None or getattr(row, "outcome", None) is None:
                raise RecoveryFailed("missing call/outcome")
            turn = _strict_slot_index(getattr(row, "turn", None), field="turn")
            index = _strict_slot_index(getattr(row, "tool_index", None), field="tool_index")
            if (turn, index) in seen:
                raise RecoveryFailed(f"duplicate recovered slot: turn={turn} index={index}")
            seen.add((turn, index))
        except RecoveryFailed:
            raise
        except Exception as exc:
            raise RecoveryFailed(f"malformed recovered row: {exc}") from exc


def _sort_recovered_rows(raw_rows: list[Any]) -> list[Any]:
    try:
        return sorted(
            raw_rows,
            key=lambda r: (int(getattr(r, "turn", 0) or 0), int(getattr(r, "tool_index", 0) or 0)),
        )
    except Exception as exc:
        raise RecoveryFailed(f"recovered row ordering failed: {exc}") from exc


def _delivery_identity(frame: Any, effect_scope: str, row: Any) -> str:
    """Return the stable turn-slot key used by ``delivered_from_frame``.

    Effect ids identify durable external effects.  Delivery maps identify the
    provider turn slot that must receive a result after recovery.  They are
    deliberately different namespaces; using an effect id here silently
    drops a valid recovered result from the continuation prompt.
    """
    return turn_effect_id(
        f"{frame.run_id}:{effect_scope}" if effect_scope else frame.run_id,
        int(row.turn),
        int(row.tool_index),
    )


def _replay_recovered_facts(session: Any, recovered_rows: list[Any], recovered_results: list[Any],
                            research_ledger: Any = None) -> None:
    """Replay facts from the single-built recovered results (no rebuild)."""
    for row, prior in zip(recovered_rows, recovered_results, strict=True):
        try:
            project_result_observation(session, row.call, prior, ok=frame_outcome_ok(row),
                                       exit_code=frame_outcome_exit_code(row), research_ledger=research_ledger)
        except Exception as exc:
            raise RecoveryFailed(f"recovered facts replay failed: {exc}") from exc


def restore_task_session(frame: Any, session: Any, *, effect_scope: str = "task", research_ledger: Any = None) -> tuple[dict[str, Any], list[Any], int, list[Any]]:
    """Rebuild recovery state; any failure raises RecoveryFailed (fail-closed).

    Single construction: ``delivered_from_frame`` builds each row once via
    the unified builder; facts and ``initial_results`` reuse those exact
    objects so facts/initial can never drift into two versions.
    """
    original_session = session
    # Fact recording appends/replaces container entries, never mutates stored
    # result objects. Shallow copies isolate the projection without copying
    # callbacks, immutable policy, or trusted workspace capabilities.
    session = session.clone_projection()
    staged_ledger = research_ledger.clone() if research_ledger is not None else None
    raw_rows = list(getattr(frame, "recovered_tool_outcomes", ()) or ())
    _validate_recovered_rows(raw_rows)
    recovered_rows = _sort_recovered_rows(raw_rows)
    try:
        delivered = delivered_from_frame(frame, effect_scope=effect_scope)
    except RecoveryFailed:
        raise
    except Exception as exc:
        raise RecoveryFailed(f"recovery delivery failed: {exc}") from exc
    # Reuse the single-built objects in sorted order for facts + initial.
    try:
        identities = [_delivery_identity(frame, effect_scope, row) for row in recovered_rows]
        recovered_results = [delivered[ident] for ident in identities]
    except Exception as exc:
        raise RecoveryFailed(f"recovered delivery map incomplete: {exc}") from exc
    # Facts outlive delivery. Reuse pending result objects. A settled row is
    # also a delivery input only when its batch is still awaiting provider
    # acknowledgement; once the batch is delivered it remains a fact only.
    fact_rows = list(getattr(frame, "settled_tool_outcomes", ()) or ())
    pending_by_effect = {r.effect_id: result for r, result in zip(recovered_rows, recovered_results, strict=True)
                         if getattr(r, "effect_id", "")}
    known = {getattr(row, "effect_id", "") for row in fact_rows if getattr(row, "effect_id", "")}
    fact_rows.extend(row for row in recovered_rows if not getattr(row, "effect_id", "")
                     or row.effect_id not in known)
    for row in fact_rows:
        _validate_recovered_rows([row])
        identity = getattr(row, "effect_id", "") or turn_effect_id(f"{frame.run_id}:{effect_scope}", row.turn, row.tool_index)
        if identity in session.restored_effect_ids or identity in session.executed:
            continue
        prior = pending_by_effect.get(identity)
        if prior is None:
            prior = build_recovered_result(spec_for_recovered_row(row))
        _replay_recovered_facts(session, [row], [prior], staged_ledger)
        session._memory_results[identity] = prior
        session.restored_effect_ids.add(identity)
    pending_batch = bool(getattr(frame, "recovered_tool_result_batch_id", "") or "")
    if pending_batch:
        for row in fact_rows:
            identity = _delivery_identity(frame, effect_scope, row)
            if identity in delivered:
                continue
            try:
                delivered[identity] = build_recovered_result(spec_for_recovered_row(row))
            except Exception as exc:
                raise RecoveryFailed(f"settled delivery rebuild failed: {exc}") from exc
    initial_rows = [
        row for row in fact_rows
        if pending_batch or row in recovered_rows
    ]
    initial_results: list[Any] = []
    for row in initial_rows:
        identity = _delivery_identity(frame, effect_scope, row)
        result = delivered.get(identity)
        if result is not None:
            initial_results.append(result)
    try:
        resume_start = max(int(getattr(r, "turn", 0) or 0) for r in fact_rows) + 1 if fact_rows else 1
        resume_start = max(1, resume_start)
    except Exception as exc:
        raise RecoveryFailed(f"recovered resume turn unreadable: {exc}") from exc
    if staged_ledger is not None:
        research_ledger.commit_projection(staged_ledger)
    original_session.commit_projection(session)
    return delivered, recovered_rows, resume_start, initial_results
