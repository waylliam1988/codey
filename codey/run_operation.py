"""Durable program counter for one coding run's lifecycle (0.5.1).

The completion/repair state of a project run used to live only on
``_run_project_mode``'s function stack (``repaired_once``, ``blocked_reason``,
``remaining_turns``, the admission payload). After a crash, a user stop, or a
provider failure, that state was gone and recovery had to guess the run's
position from missing events.

``RunOperationState`` is the borrowed-from-pi answer, sized to Codey: one
small register per run -- ``state/run_operations/<session_key>/<run_id>.json``
-- overwritten at every committed phase boundary with the *total* current
state. Recovery reads the register and switches on it; it never replays a
journal and never infers position from absence. The terminal commit keeps one
bounded terminal snapshot (roadmap 0.5.1); it never becomes a history.

Boundaries:

- refs/status/counts/reasons only. No raw prompt, raw reply, raw
  stdout/stderr, raw diff, source body, repair prompt text, or provider error
  text exists anywhere in the payload, and every free-text field is clipped.
- the transition table is closed. ``repair_running`` cannot be reached without
  a committed ``repair_context_admitted``; ``repair_rounds`` cannot exceed the
  budget handed in at ``start()``; terminal is immutable except for the
  exact-same-payload idempotent re-commit.
- the reader fails closed. Bad schema, wrong ids, unknown phase, or an
  oversize file load as ``None``; there is no migration and no legacy guess --
  cold start, schema v1 only.
- this module is a storage leaf: stdlib plus ``codey.storage`` and nothing
  else. It never imports agents, providers, tools, server, or ghost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import (
    DEFAULT_STATE_HOME,
    read_json,
    session_key,
    write_json_atomic,
)


SCHEMA_VERSION = 1
KIND = "run_operation_state"
MAX_OPERATION_BYTES = 64 * 1024

PHASE_ACCEPTED = "accepted"
PHASE_WRITER_RUNNING = "writer_running"
PHASE_WRITER_SETTLED = "writer_settled"
PHASE_COMPLETION_PROOF_RECORDED = "completion_proof_recorded"
PHASE_REPAIR_CONTEXT_ADMITTED = "repair_context_admitted"
PHASE_REPAIR_RUNNING = "repair_running"
PHASE_REPAIR_SETTLED = "repair_settled"
PHASE_TERMINAL = "terminal"

PHASES = frozenset(
    {
        PHASE_ACCEPTED,
        PHASE_WRITER_RUNNING,
        PHASE_WRITER_SETTLED,
        PHASE_COMPLETION_PROOF_RECORDED,
        PHASE_REPAIR_CONTEXT_ADMITTED,
        PHASE_REPAIR_RUNNING,
        PHASE_REPAIR_SETTLED,
        PHASE_TERMINAL,
    }
)

# Every non-terminal phase may go straight to terminal: a user stop or a
# provider failure is honest from any position. The repair arm is locked:
# proof -> admitted -> running -> settled, and a settled repair either
# re-records the final proof or ends the run.
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PHASE_ACCEPTED: frozenset({PHASE_WRITER_RUNNING, PHASE_TERMINAL}),
    PHASE_WRITER_RUNNING: frozenset({PHASE_WRITER_SETTLED, PHASE_TERMINAL}),
    PHASE_WRITER_SETTLED: frozenset({PHASE_COMPLETION_PROOF_RECORDED, PHASE_TERMINAL}),
    PHASE_COMPLETION_PROOF_RECORDED: frozenset(
        {
            PHASE_REPAIR_CONTEXT_ADMITTED,
            PHASE_TERMINAL,
        }
    ),
    PHASE_REPAIR_CONTEXT_ADMITTED: frozenset({PHASE_REPAIR_RUNNING, PHASE_TERMINAL}),
    PHASE_REPAIR_RUNNING: frozenset({PHASE_REPAIR_SETTLED, PHASE_TERMINAL}),
    PHASE_REPAIR_SETTLED: frozenset({PHASE_COMPLETION_PROOF_RECORDED, PHASE_TERMINAL}),
    PHASE_TERMINAL: frozenset({PHASE_TERMINAL}),
}

MAX_TEXT_CHARS = 80
MAX_REF_CHARS = 120
MAX_PROJECT_REF_CHARS = 240

INTERRUPTED_WRITING = "Writing was interrupted"
INTERRUPTED_COMPLETION_CHECK = "Completion check was interrupted"
INTERRUPTED_REPAIR = "Stopped during repair"


class RunOperationTransitionError(Exception):
    """A phase transition violated the closed transition table."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class RunOperationTerminal:
    """Bounded terminal snapshot; mirrors ``RunLedger.finish`` fields."""

    stop_reason: str
    summary_chars: int
    turns: int
    max_turns: int
    provider: str
    blocked_reason: str
    finished_at: str

    def to_payload(self) -> dict[str, object]:
        return {
            "stop_reason": self.stop_reason,
            "summary_chars": self.summary_chars,
            "turns": self.turns,
            "max_turns": self.max_turns,
            "provider": self.provider,
            "blocked_reason": self.blocked_reason,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "RunOperationTerminal | None":
        if not isinstance(payload, dict):
            return None
        return cls(
            stop_reason=_clip(payload.get("stop_reason"), MAX_TEXT_CHARS),
            summary_chars=_nonnegative_int(payload.get("summary_chars")),
            turns=_nonnegative_int(payload.get("turns")),
            max_turns=_nonnegative_int(payload.get("max_turns")),
            provider=_clip(payload.get("provider"), MAX_TEXT_CHARS),
            blocked_reason=_clip(payload.get("blocked_reason"), MAX_TEXT_CHARS),
            finished_at=_clip(payload.get("finished_at"), 40),
        )

    def identity(self) -> "RunOperationTerminal":
        """The terminal minus its timestamp, for the idempotent re-commit."""

        return replace(self, finished_at="")


@dataclass(frozen=True)
class RunOperationState:
    """Total current state of one run's completion/repair lifecycle."""

    session_id: str
    run_id: str
    project_ref: str
    provider_id: str
    turn_budget: int
    max_repair_rounds: int
    phase: str
    started_at: str
    updated_at: str
    writer_attempt: int = 1
    turns_used: int = 0
    stop_reason: str = ""
    completion_proof_ref: str = ""
    completion_proof_status: str = ""
    completion_proof_satisfied: bool | None = None
    repair_rounds: int = 0
    repair_context_ref: str = ""
    blocked_reason: str = ""
    terminal: RunOperationTerminal | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "project_ref": self.project_ref,
            "provider_id": self.provider_id,
            "turn_budget": self.turn_budget,
            "max_repair_rounds": self.max_repair_rounds,
            "phase": self.phase,
            "writer_attempt": self.writer_attempt,
            "turns_used": self.turns_used,
            "stop_reason": self.stop_reason,
            "completion_proof_ref": self.completion_proof_ref,
            "completion_proof_status": self.completion_proof_status,
            "repair_rounds": self.repair_rounds,
            "repair_context_ref": self.repair_context_ref,
            "blocked_reason": self.blocked_reason,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }
        if self.completion_proof_satisfied is not None:
            payload["completion_proof_satisfied"] = self.completion_proof_satisfied
        if self.terminal is not None:
            payload["terminal"] = self.terminal.to_payload()
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> "RunOperationState | None":
        """Fail closed: anything unexpected loads as ``None``."""

        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != SCHEMA_VERSION or payload.get("kind") != KIND:
            return None
        session_id = _clip(payload.get("session_id"), 200)
        run_id = _clip(payload.get("run_id"), 200)
        phase = _clip(payload.get("phase"), 40)
        if not session_id or not run_id or phase not in PHASES:
            return None
        satisfied = payload.get("completion_proof_satisfied")
        if satisfied is not None and not isinstance(satisfied, bool):
            return None
        terminal = None
        if phase == PHASE_TERMINAL:
            terminal = RunOperationTerminal.from_payload(payload.get("terminal"))
            if terminal is None:
                return None
        elif "terminal" in payload:
            return None
        return cls(
            session_id=session_id,
            run_id=run_id,
            project_ref=_clip(payload.get("project_ref"), MAX_PROJECT_REF_CHARS),
            provider_id=_clip(payload.get("provider_id"), MAX_TEXT_CHARS),
            turn_budget=_nonnegative_int(payload.get("turn_budget")),
            max_repair_rounds=_nonnegative_int(payload.get("max_repair_rounds")),
            phase=phase,
            started_at=_clip(payload.get("started_at"), 40),
            updated_at=_clip(payload.get("updated_at"), 40),
            writer_attempt=max(1, _nonnegative_int(payload.get("writer_attempt")) or 1),
            turns_used=_nonnegative_int(payload.get("turns_used")),
            stop_reason=_clip(payload.get("stop_reason"), MAX_TEXT_CHARS),
            completion_proof_ref=_clip(payload.get("completion_proof_ref"), MAX_REF_CHARS),
            completion_proof_status=_clip(payload.get("completion_proof_status"), MAX_TEXT_CHARS),
            completion_proof_satisfied=satisfied,
            repair_rounds=_nonnegative_int(payload.get("repair_rounds")),
            repair_context_ref=_clip(payload.get("repair_context_ref"), MAX_REF_CHARS),
            blocked_reason=_clip(payload.get("blocked_reason"), MAX_TEXT_CHARS),
            terminal=terminal,
        )


def operation_progress_text(state: RunOperationState | None) -> str:
    """The quiet Details line for a run that never reached terminal."""

    if state is None or state.phase == PHASE_TERMINAL:
        return ""
    if state.phase in (PHASE_ACCEPTED, PHASE_WRITER_RUNNING):
        return INTERRUPTED_WRITING
    if state.phase in (PHASE_WRITER_SETTLED, PHASE_COMPLETION_PROOF_RECORDED):
        return INTERRUPTED_COMPLETION_CHECK
    return INTERRUPTED_REPAIR


def _transition(state: RunOperationState, next_phase: str, **updates: object) -> RunOperationState:
    if next_phase not in _ALLOWED_TRANSITIONS.get(state.phase, frozenset()):
        raise RunOperationTransitionError(f"illegal transition {state.phase} -> {next_phase}")
    return replace(state, phase=next_phase, updated_at=_now(), **updates)  # type: ignore[arg-type]


def mark_writer_running(
    state: RunOperationState,
    *,
    provider_id: str,
    writer_attempt: int = 1,
) -> RunOperationState:
    return _transition(
        state,
        PHASE_WRITER_RUNNING,
        provider_id=_clip(provider_id, MAX_TEXT_CHARS),
        writer_attempt=max(1, _nonnegative_int(writer_attempt) or 1),
    )


def mark_writer_settled(
    state: RunOperationState,
    *,
    provider_id: str,
    turns_used: int,
    stop_reason: str,
) -> RunOperationState:
    return _transition(
        state,
        PHASE_WRITER_SETTLED,
        provider_id=_clip(provider_id, MAX_TEXT_CHARS),
        turns_used=_nonnegative_int(turns_used),
        stop_reason=_clip(stop_reason, MAX_TEXT_CHARS),
    )


def mark_completion_proof_recorded(
    state: RunOperationState,
    *,
    proof_ref: str,
    proof_status: str,
    proof_satisfied: bool | None,
) -> RunOperationState:
    return _transition(
        state,
        PHASE_COMPLETION_PROOF_RECORDED,
        completion_proof_ref=_clip(proof_ref, MAX_REF_CHARS),
        completion_proof_status=_clip(proof_status, MAX_TEXT_CHARS),
        completion_proof_satisfied=proof_satisfied,
    )


def mark_repair_context_admitted(
    state: RunOperationState,
    *,
    context_ref: str,
) -> RunOperationState:
    return _transition(
        state,
        PHASE_REPAIR_CONTEXT_ADMITTED,
        repair_context_ref=_clip(context_ref, MAX_REF_CHARS),
    )


def mark_repair_running(state: RunOperationState, *, provider_id: str) -> RunOperationState:
    if state.repair_rounds >= state.max_repair_rounds:
        raise RunOperationTransitionError("repair round budget is exhausted")
    return _transition(
        state,
        PHASE_REPAIR_RUNNING,
        provider_id=_clip(provider_id, MAX_TEXT_CHARS),
        repair_rounds=state.repair_rounds + 1,
    )


def mark_repair_settled(
    state: RunOperationState,
    *,
    provider_id: str,
    stop_reason: str,
    blocked_reason: str = "",
    turns_used: int | None = None,
) -> RunOperationState:
    return _transition(
        state,
        PHASE_REPAIR_SETTLED,
        provider_id=_clip(provider_id, MAX_TEXT_CHARS),
        stop_reason=_clip(stop_reason, MAX_TEXT_CHARS),
        blocked_reason=_clip(blocked_reason, MAX_TEXT_CHARS),
        turns_used=(state.turns_used if turns_used is None else _nonnegative_int(turns_used)),
    )


def mark_completion_blocked(
    state: RunOperationState,
    *,
    reason: str,
) -> RunOperationState:
    """Record the enforcement decision on the already-recorded proof.

    The decision point runs after ``completion_proof_recorded``; it updates
    the verdict field without inventing a ninth phase.
    """

    if state.phase != PHASE_COMPLETION_PROOF_RECORDED:
        raise RunOperationTransitionError(
            f"completion verdict requires phase {PHASE_COMPLETION_PROOF_RECORDED}, not {state.phase}"
        )
    return replace(state, blocked_reason=_clip(reason, MAX_TEXT_CHARS), updated_at=_now())


def mark_terminal(
    state: RunOperationState,
    *,
    stop_reason: str,
    summary_chars: int,
    turns: int,
    max_turns: int,
    provider: str,
    blocked_reason: str | None = None,
) -> RunOperationState:
    terminal = RunOperationTerminal(
        stop_reason=_clip(stop_reason, MAX_TEXT_CHARS),
        summary_chars=_nonnegative_int(summary_chars),
        turns=_nonnegative_int(turns),
        max_turns=_nonnegative_int(max_turns),
        provider=_clip(provider, MAX_TEXT_CHARS),
        blocked_reason=_clip(state.blocked_reason if blocked_reason is None else blocked_reason, MAX_TEXT_CHARS),
        finished_at=_now(),
    )
    if state.phase == PHASE_TERMINAL:
        if state.terminal is not None and state.terminal.identity() == terminal.identity():
            return state
        raise RunOperationTransitionError("terminal state is immutable")
    return _transition(
        state,
        PHASE_TERMINAL,
        terminal=terminal,
        blocked_reason=terminal.blocked_reason,
    )


def _safe_file_stem(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())[:120].strip("._")
    return text or "run"


class RunOperationStore:
    """Persistence only: read, lock, transition, atomically overwrite."""

    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.state_home = Path(state_home)

    def root_dir(self) -> Path:
        return self.state_home / "run_operations"

    def session_dir(self, session_id: str) -> Path:
        return self.root_dir() / session_key(session_id)

    def path_for(self, session_id: str, run_id: str) -> Path:
        return self.session_dir(session_id) / f"{_safe_file_stem(run_id)}.json"

    def start(
        self,
        *,
        session_id: str,
        run_id: str,
        project_ref: str,
        provider_id: str,
        turn_budget: int,
        max_repair_rounds: int,
    ) -> RunOperationState | None:
        if self.load(session_id, run_id) is not None:
            return None
        now = _now()
        state = RunOperationState(
            session_id=str(session_id or ""),
            run_id=str(run_id or ""),
            project_ref=_clip(project_ref, MAX_PROJECT_REF_CHARS),
            provider_id=_clip(provider_id, MAX_TEXT_CHARS),
            turn_budget=_nonnegative_int(turn_budget),
            max_repair_rounds=_nonnegative_int(max_repair_rounds),
            phase=PHASE_ACCEPTED,
            started_at=now,
            updated_at=now,
        )
        try:
            write_json_atomic(
                self.path_for(session_id, run_id),
                state.to_payload(),
                max_bytes=MAX_OPERATION_BYTES,
            )
        except (OSError, ValueError):
            return None
        return state

    def load(self, session_id: str, run_id: str) -> RunOperationState | None:
        payload = read_json(self.path_for(session_id, run_id), max_bytes=MAX_OPERATION_BYTES)
        if payload is None:
            return None
        state = RunOperationState.from_payload(payload)
        if state is None or state.session_id != str(session_id or "") or state.run_id != str(run_id or ""):
            return None
        return state

    def commit(
        self,
        session_id: str,
        run_id: str,
        transition: "Callable[[RunOperationState], RunOperationState]",
    ) -> RunOperationState | None:
        path = self.path_for(session_id, run_id)
        with with_file_lock(path):
            current = self.load(session_id, run_id)
            if current is None:
                return None
            next_state = transition(current)
            if next_state == current:
                return current
            write_json_atomic(
                path,
                next_state.to_payload(),
                max_bytes=MAX_OPERATION_BYTES,
            )
        return next_state

    def delete_session(self, session_id: str) -> None:
        bucket = self.session_dir(session_id)
        try:
            for path in bucket.glob("*.json"):
                path.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = [
    "INTERRUPTED_COMPLETION_CHECK",
    "INTERRUPTED_REPAIR",
    "INTERRUPTED_WRITING",
    "KIND",
    "MAX_OPERATION_BYTES",
    "PHASE_ACCEPTED",
    "PHASE_COMPLETION_PROOF_RECORDED",
    "PHASE_REPAIR_CONTEXT_ADMITTED",
    "PHASE_REPAIR_RUNNING",
    "PHASE_REPAIR_SETTLED",
    "PHASE_TERMINAL",
    "PHASE_WRITER_RUNNING",
    "PHASE_WRITER_SETTLED",
    "PHASES",
    "SCHEMA_VERSION",
    "RunOperationState",
    "RunOperationStore",
    "RunOperationTerminal",
    "RunOperationTransitionError",
    "mark_completion_blocked",
    "mark_completion_proof_recorded",
    "mark_repair_context_admitted",
    "mark_repair_running",
    "mark_repair_settled",
    "mark_terminal",
    "mark_writer_running",
    "mark_writer_settled",
    "operation_progress_text",
]
