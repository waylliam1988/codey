"""Restore shared task facts and pending delivery from durable outcomes."""
from __future__ import annotations

from dataclasses import fields, replace
from typing import Any

from codey.operations.kernel_errors import RecoveryFailed
from codey.operations.kernel_facts import record_facts_for_result
from codey.operations.kernel_recovery_result import (
    _strict_slot_index,
    build_recovered_result,
    frame_outcome_exit_code,
    frame_outcome_ok,
    spec_for_recovered_row,
)
from codey.operations.recovery import delivered_from_frame
from codey.operations.task_session import turn_effect_id
from codey.research.ledger_receipts import restore_ledger_observation


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


def _replay_recovered_facts(session: Any, recovered_rows: list[Any], recovered_results: list[Any]) -> None:
    """Replay facts from the single-built recovered results (no rebuild)."""
    for row, prior in zip(recovered_rows, recovered_results, strict=True):
        try:
            record_facts_for_result(session, row.call, prior, ok=frame_outcome_ok(row),
                                    exit_code=frame_outcome_exit_code(row))
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
    session = replace(session, **{name: value.copy() for name, value in vars(session).items()
                                  if type(value) in {dict, list, set}})
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
        identities = [
            turn_effect_id(f"{frame.run_id}:{effect_scope}", int(r.turn), int(r.tool_index))
            for r in recovered_rows
        ]
        recovered_results = [delivered[ident] for ident in identities]
    except Exception as exc:
        raise RecoveryFailed(f"recovered delivery map incomplete: {exc}") from exc
    # Facts outlive delivery. Reuse pending result objects, but do not send
    # already-delivered history back as native call results.
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
        observation = prior.canonical.get("research_observation")
        if staged_ledger is not None and observation is not None:
            try:
                if not isinstance(observation, dict):
                    raise RecoveryFailed("research ledger observation is not a mapping")
                restore_ledger_observation(staged_ledger, observation)
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                raise RecoveryFailed(f"research ledger recovery failed: {exc}") from exc
        _replay_recovered_facts(session, [row], [prior])
        session._memory_results[identity] = prior
        session.restored_effect_ids.add(identity)
    try:
        resume_start = max(int(getattr(r, "turn", 0) or 0) for r in fact_rows) + 1 if fact_rows else 1
        resume_start = max(1, resume_start)
    except Exception as exc:
        raise RecoveryFailed(f"recovered resume turn unreadable: {exc}") from exc
    if staged_ledger is not None:
        research_ledger.commit_projection(staged_ledger)
    for item in fields(session):
        setattr(original_session, item.name, getattr(session, item.name))
    return delivered, recovered_rows, resume_start, list(recovered_results)
