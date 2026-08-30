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
  stdout/stderr, raw diff, source body, repair prompt text, provider error
  text, or raw project path exists anywhere in the payload; the project is
  named by a stable ``project:<key>`` ref, the repair context by its
  ``sha256:<hex>`` digest, and the proof by its ``completion_proof:<hex>``
  id with one of the recorded proof statuses.
- the transition table is closed. ``repair_running`` cannot be reached without
  a committed ``repair_context_admitted``; ``repair_rounds`` cannot exceed the
  budget handed in at ``start()``; terminal is immutable except for the
  exact-same-payload idempotent re-commit.
- the reader fails closed and canonicalizes nothing. Bad schema, wrong ids,
  unknown phase, unknown keys (top-level or inside the terminal snapshot),
  wrong JSON types (bool-as-int, numeric strings), missing fields, padded
  text fields, raw or malformed refs, impossible phase states (repair or
  proof facts before the phases that produce them, repair phases not
  carrying the failed proof that admitted them, post-proof phases and
  terminal without the facts their source phase committed, a re-proof with
  a partial repair record, a blocked verdict outside the phases that may
  carry one or without its unsatisfied failed/blocked proof, proof refs or
  statuses outside the recorded-proof contract, rounds over budget), or an
  oversize file load as ``None``; there is no migration, no coercion,
  and no legacy guess -- cold start, canonical schema v1 only.
- the writer is held to the reader's bar. The transition helpers validate
  every fact exactly as the reader will -- nothing is clipped or coerced, a
  non-canonical fact raises ``RunOperationTransitionError`` -- and
  ``commit()`` re-derives the canonical schema from the candidate before it
  may touch the disk, refusing a register whose identity would move.
  ``start()`` never clips or clobbers: a non-canonical argument is refused
  without writing, and an existing file -- valid or not -- refuses a second
  start under the same file lock that ``commit()`` uses.
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
    project_key,
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
MAX_ID_CHARS = 200

INTERRUPTED_WRITING = "Writing was interrupted"
INTERRUPTED_COMPLETION_CHECK = "Completion check was interrupted"
INTERRUPTED_FINISHING = "Finishing was interrupted"
INTERRUPTED_REPAIR = "Stopped during repair"

# Phase invariants the reader enforces on top of the type checks: the
# register may only claim what the closed transition table could have
# produced at this phase. Pre-repair phases carry no repair or proof facts;
# every phase the table can only reach through a committed completion proof
# carries that proof's complete facts.
_PRE_REPAIR_PHASES = frozenset(
    {PHASE_ACCEPTED, PHASE_WRITER_RUNNING, PHASE_WRITER_SETTLED}
)
_REPAIR_PHASES = frozenset(
    {PHASE_REPAIR_CONTEXT_ADMITTED, PHASE_REPAIR_RUNNING, PHASE_REPAIR_SETTLED}
)
_REPAIR_EXECUTING_PHASES = frozenset({PHASE_REPAIR_RUNNING, PHASE_REPAIR_SETTLED})
_POST_PROOF_PHASES = frozenset({PHASE_COMPLETION_PROOF_RECORDED}) | _REPAIR_PHASES
# A blocked verdict is final: only the phase that records the decision, the
# settled repair that failed on its own, and terminal may carry one.
_VERDICT_PHASES = frozenset(
    {PHASE_COMPLETION_PROOF_RECORDED, PHASE_REPAIR_SETTLED, PHASE_TERMINAL}
)

# The key set is closed: a payload carrying anything else -- an "extension"
# field, a raw prompt, a diff -- is not schema v1 and fails closed.
_KNOWN_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "session_id",
        "run_id",
        "project_ref",
        "provider_id",
        "turn_budget",
        "max_repair_rounds",
        "phase",
        "writer_attempt",
        "turns_used",
        "stop_reason",
        "completion_proof_ref",
        "completion_proof_status",
        "completion_proof_satisfied",
        "repair_rounds",
        "repair_context_ref",
        "blocked_reason",
        "started_at",
        "updated_at",
        "terminal",
    }
)
# The terminal snapshot's key set is closed too: an extra field inside
# ``terminal`` is an extension, not schema v1, and fails the whole payload.
_KNOWN_TERMINAL_KEYS = frozenset(
    {
        "stop_reason",
        "summary_chars",
        "turns",
        "max_turns",
        "provider",
        "blocked_reason",
        "finished_at",
    }
)
_PROJECT_REF_RE = re.compile(r"^project:[0-9a-f]{24}$")
_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# The recorded proof's own closed contract, mirroring the completion trace's
# proof vocabulary: a stable ``completion_proof:<16 hex>`` id, one of the
# final proof statuses (never pending/running), and a satisfied flag derived
# from the status -- the invariant the proof builder itself guarantees.
_COMPLETION_PROOF_REF_RE = re.compile(r"^completion_proof:[0-9a-f]{16}$")
_RECORDED_PROOF_STATUSES = frozenset(
    {"complete", "complete_with_limitations", "failed", "blocked"}
)
_BLOCKABLE_PROOF_STATUSES = frozenset({"failed", "blocked"})
# The repair arm exists for product failures: the completion projection
# admits a context for unsatisfied failed proofs only.
_REPAIR_SOURCE_PROOF_STATUSES = frozenset({"failed"})


class RunOperationTransitionError(Exception):
    """A phase transition violated the closed transition table."""


class _SchemaError(ValueError):
    """The payload is not canonical schema v1; the reader fails closed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _strict_text(value: object, *, field: str, limit: int, allow_empty: bool = False) -> str:
    """The writer's mirror of the reader: canonical fact or refused write.

    Facts are validated, never clipped or coerced -- anything the reader
    could not load back must fail the transition here, not become a
    register the next ``load()`` rejects.
    """

    if not isinstance(value, str):
        raise RunOperationTransitionError(f"{field} must be a string")
    text = value.strip()
    if text != value:
        raise RunOperationTransitionError(f"{field} must be canonical (unpadded)")
    if not text and not allow_empty:
        raise RunOperationTransitionError(f"{field} must not be empty")
    if len(text) > limit:
        raise RunOperationTransitionError(f"{field} exceeds {limit} chars")
    return text


def _strict_count(value: object, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RunOperationTransitionError(f"{field} must be an int of at least {minimum}")
    return value


def _canonical_id(value: object) -> str:
    """The one canonical identity form, or "" when the id is not canonical.

    Identity is never clipped: a register written under a trimmed id could
    never be found again by commits keyed on the original id. Callers hand
    in a non-empty string, at most ``MAX_ID_CHARS`` chars, equal to its own
    strip -- anything else fails closed at the boundary.
    """

    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > MAX_ID_CHARS or text != value:
        return ""
    return text


def _valid_project_ref(ref: str) -> bool:
    return not ref or _PROJECT_REF_RE.match(ref) is not None


def _text_field(
    payload: dict,
    key: str,
    *,
    limit: int,
    allow_empty: bool = False,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise _SchemaError(f"{key} must be a string")
    text = value.strip()
    if text != value:
        # The reader canonicalizes nothing: a padded field is not schema v1.
        raise _SchemaError(f"{key} must be canonical (unpadded)")
    if not text and not allow_empty:
        raise _SchemaError(f"{key} must not be empty")
    if len(text) > limit:
        raise _SchemaError(f"{key} exceeds {limit} chars")
    return text


def _int_field(payload: dict, key: str) -> int:
    value = payload.get(key)
    # bool is an int subclass; True must never pass as 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _SchemaError(f"{key} must be a non-negative int")
    return value


def _project_ref(project: object) -> str:
    """Name the project by a stable ref, never by its raw absolute path."""

    text = str(project or "").strip()
    if not text:
        return ""
    return f"project:{project_key(text)}"


def _proof_facts_claimed(proof_ref: str, proof_status: str, satisfied: object) -> bool:
    """Whether any part of the recorded proof is claimed."""

    return bool(proof_ref) or bool(proof_status) or satisfied is not None


def _proof_facts_complete(proof_ref: str, proof_status: str, satisfied: object) -> bool:
    """Whether the payload carries the complete recorded-proof triple."""

    return bool(proof_ref) and bool(proof_status) and satisfied is not None


def _repair_facts_claimed(repair_rounds: int, repair_context_ref: str) -> bool:
    """Whether any part of the repair arm is claimed."""

    return repair_rounds > 0 or bool(repair_context_ref)


def _blocked_verdict_supported(proof_ref: str, proof_status: str, satisfied: object) -> bool:
    """Whether an unsatisfied failed/blocked proof backs a blocked verdict.

    A run blocked by the completion decision carries the verdict on the
    proof that failed it -- never on a complete, limited, or unproven run.
    """

    return (
        _proof_facts_complete(proof_ref, proof_status, satisfied)
        and proof_status in _BLOCKABLE_PROOF_STATUSES
        and satisfied is False
    )


def _repair_candidate_proof(proof_ref: str, proof_status: str, satisfied: object) -> bool:
    """Whether the recorded proof can admit a repair context.

    Only an unsatisfied failed proof sends the run into the repair arm; a
    complete or limited pass needs no repair and a blocked one cannot run
    one.
    """

    return (
        _proof_facts_complete(proof_ref, proof_status, satisfied)
        and proof_status in _REPAIR_SOURCE_PROOF_STATUSES
        and satisfied is False
    )


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
        try:
            if set(payload) - _KNOWN_TERMINAL_KEYS:
                raise _SchemaError("terminal payload carries unknown keys")
            return cls(
                stop_reason=_text_field(payload, "stop_reason", limit=MAX_TEXT_CHARS, allow_empty=True),
                summary_chars=_int_field(payload, "summary_chars"),
                turns=_int_field(payload, "turns"),
                max_turns=_int_field(payload, "max_turns"),
                provider=_text_field(payload, "provider", limit=MAX_TEXT_CHARS, allow_empty=True),
                blocked_reason=_text_field(
                    payload, "blocked_reason", limit=MAX_TEXT_CHARS, allow_empty=True
                ),
                finished_at=_text_field(payload, "finished_at", limit=40),
            )
        except _SchemaError:
            return None

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
        """Fail closed: anything not canonical schema v1 loads as ``None``."""

        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != SCHEMA_VERSION or payload.get("kind") != KIND:
            return None
        try:
            if set(payload) - _KNOWN_PAYLOAD_KEYS:
                raise _SchemaError("payload carries unknown keys")
            session_id = _text_field(payload, "session_id", limit=MAX_ID_CHARS)
            run_id = _text_field(payload, "run_id", limit=MAX_ID_CHARS)
            phase = _text_field(payload, "phase", limit=40)
            if phase not in PHASES:
                raise _SchemaError(f"unknown phase {phase}")
            if "completion_proof_satisfied" in payload:
                # Missing and explicit null are different payloads: the
                # writer omits the key when there is no proof, so a null is
                # never schema v1.
                satisfied = payload["completion_proof_satisfied"]
                if not isinstance(satisfied, bool):
                    raise _SchemaError("completion_proof_satisfied must be a bool")
            else:
                satisfied = None
            project_ref = _text_field(
                payload, "project_ref", limit=MAX_PROJECT_REF_CHARS, allow_empty=True
            )
            if not _valid_project_ref(project_ref):
                raise _SchemaError("project_ref must be empty or a project:<key> ref")
            max_repair_rounds = _int_field(payload, "max_repair_rounds")
            repair_rounds = _int_field(payload, "repair_rounds")
            if repair_rounds > max_repair_rounds:
                raise _SchemaError("repair_rounds exceeds the repair budget")
            repair_context_ref = _text_field(
                payload, "repair_context_ref", limit=MAX_REF_CHARS, allow_empty=True
            )
            if repair_context_ref and not _SHA256_REF_RE.match(repair_context_ref):
                raise _SchemaError("repair_context_ref must be a sha256:<64 hex> digest ref")
            writer_attempt = _int_field(payload, "writer_attempt")
            if writer_attempt < 1:
                raise _SchemaError("writer_attempt must be at least 1")
            turns_used = _int_field(payload, "turns_used")
            stop_reason = _text_field(
                payload, "stop_reason", limit=MAX_TEXT_CHARS, allow_empty=True
            )
            proof_ref = _text_field(
                payload, "completion_proof_ref", limit=MAX_REF_CHARS, allow_empty=True
            )
            proof_status = _text_field(
                payload, "completion_proof_status", limit=MAX_TEXT_CHARS, allow_empty=True
            )
            if proof_ref and not _COMPLETION_PROOF_REF_RE.match(proof_ref):
                raise _SchemaError("completion_proof_ref must be a completion_proof:<16 hex> ref")
            if proof_status and proof_status not in _RECORDED_PROOF_STATUSES:
                raise _SchemaError("completion_proof_status is not a recorded status")
            # Phase invariants: the register may only claim what the closed
            # transition table could have produced at this phase.
            if phase in _REPAIR_PHASES:
                if not repair_context_ref:
                    raise _SchemaError(f"{phase} requires a committed repair context ref")
                if proof_status != "failed":
                    # The arm is reachable only from an unsatisfied failed
                    # proof, and it carries that proof throughout.
                    raise _SchemaError(f"{phase} requires the failed proof that admitted it")
            if phase in _REPAIR_EXECUTING_PHASES and repair_rounds < 1:
                raise _SchemaError(f"{phase} requires a committed repair round")
            if phase in _PRE_REPAIR_PHASES:
                if _repair_facts_claimed(repair_rounds, repair_context_ref):
                    raise _SchemaError(f"{phase} cannot carry repair facts")
                if _proof_facts_claimed(proof_ref, proof_status, satisfied):
                    raise _SchemaError(f"{phase} cannot carry completion proof facts")
            elif phase in _POST_PROOF_PHASES:
                if not _proof_facts_complete(proof_ref, proof_status, satisfied):
                    raise _SchemaError(f"{phase} requires the recorded proof facts")
                if satisfied != (proof_status == "complete"):
                    raise _SchemaError(f"{phase} proof satisfied must match the recorded status")
                if phase == PHASE_COMPLETION_PROOF_RECORDED and _repair_facts_claimed(
                    repair_rounds, repair_context_ref
                ):
                    # The table produces only two kinds of recorded proof:
                    # the first one, with no repair facts, and the
                    # post-repair re-proof, carrying the context and at
                    # least one committed round.
                    if not repair_context_ref or repair_rounds < 1:
                        raise _SchemaError(f"{phase} carries a partial repair record")
            elif phase == PHASE_TERMINAL:
                # Terminal keeps whatever facts its source phase committed;
                # the combination must still be one the table could have
                # produced on the way in.
                proof_complete = _proof_facts_complete(proof_ref, proof_status, satisfied)
                if _proof_facts_claimed(proof_ref, proof_status, satisfied) and not proof_complete:
                    raise _SchemaError("terminal carries incomplete proof facts")
                if _repair_facts_claimed(repair_rounds, repair_context_ref) and not proof_complete:
                    raise _SchemaError("terminal carries repair facts without the recorded proof")
                if repair_rounds > 0 and not repair_context_ref:
                    raise _SchemaError("terminal carries repair rounds without the context ref")
                if repair_context_ref and repair_rounds == 0 and proof_status != "failed":
                    # repair_context_admitted -> terminal is the stop before
                    # the repair ran, and admission belongs to a failed proof.
                    raise _SchemaError(
                        "terminal carries an admitted repair context without its failed proof"
                    )
                if proof_complete and satisfied != (proof_status == "complete"):
                    raise _SchemaError("terminal proof satisfied must match the recorded status")
            # A blocked verdict is final: it may only sit on the phase that
            # records the decision, the settled repair that failed on its
            # own, and terminal -- never on an active repair phase.
            blocked_reason = _text_field(
                payload, "blocked_reason", limit=MAX_TEXT_CHARS, allow_empty=True
            )
            if blocked_reason and phase not in _VERDICT_PHASES:
                raise _SchemaError(f"{phase} cannot carry a blocked verdict")
            if blocked_reason and not _blocked_verdict_supported(
                proof_ref, proof_status, satisfied
            ):
                raise _SchemaError("blocked_reason requires an unsatisfied failed/blocked proof")
            # Writer facts are also only what the table could have produced:
            # a fresh register carries nothing, and a running writer has not
            # settled yet. writer_settled and every later phase keep their
            # honest zero/empty forms.
            if phase == PHASE_ACCEPTED and (writer_attempt != 1 or turns_used or stop_reason):
                raise _SchemaError(f"{phase} must be the fresh register start() wrote")
            if phase == PHASE_WRITER_RUNNING and (turns_used or stop_reason):
                raise _SchemaError(f"{phase} cannot carry settled writer facts")
            terminal = None
            if phase == PHASE_TERMINAL:
                terminal = RunOperationTerminal.from_payload(payload.get("terminal"))
                if terminal is None:
                    raise _SchemaError("terminal phase requires a valid terminal payload")
                if blocked_reason != terminal.blocked_reason:
                    raise _SchemaError("terminal snapshot must carry the state's blocked reason")
            elif "terminal" in payload:
                raise _SchemaError("non-terminal phase must not carry a terminal payload")
            return cls(
                session_id=session_id,
                run_id=run_id,
                project_ref=project_ref,
                provider_id=_text_field(payload, "provider_id", limit=MAX_TEXT_CHARS),
                turn_budget=_int_field(payload, "turn_budget"),
                max_repair_rounds=max_repair_rounds,
                phase=phase,
                started_at=_text_field(payload, "started_at", limit=40),
                updated_at=_text_field(payload, "updated_at", limit=40),
                writer_attempt=writer_attempt,
                turns_used=turns_used,
                stop_reason=stop_reason,
                completion_proof_ref=proof_ref,
                completion_proof_status=proof_status,
                completion_proof_satisfied=satisfied,
                repair_rounds=repair_rounds,
                repair_context_ref=repair_context_ref,
                blocked_reason=blocked_reason,
                terminal=terminal,
            )
        except _SchemaError:
            return None


def operation_progress_text(state: RunOperationState | None) -> str:
    """The quiet Details line for a run that never reached terminal.

    The wording names what was actually interrupted: a settled repair is no
    longer running one, and a satisfied proof means the run was finishing,
    not still being checked.
    """

    if state is None or state.phase == PHASE_TERMINAL:
        return ""
    if state.phase in (PHASE_ACCEPTED, PHASE_WRITER_RUNNING):
        return INTERRUPTED_WRITING
    if state.phase in (PHASE_REPAIR_CONTEXT_ADMITTED, PHASE_REPAIR_RUNNING):
        return INTERRUPTED_REPAIR
    if state.phase == PHASE_COMPLETION_PROOF_RECORDED and state.completion_proof_satisfied:
        return INTERRUPTED_FINISHING
    return INTERRUPTED_COMPLETION_CHECK


def _transition(state: RunOperationState, next_phase: str, **updates: object) -> RunOperationState:
    if next_phase not in _ALLOWED_TRANSITIONS.get(state.phase, frozenset()):
        raise RunOperationTransitionError(f"illegal transition {state.phase} -> {next_phase}")
    if state.blocked_reason and next_phase != PHASE_TERMINAL:
        # The verdict is final: a blocked register may only end the run.
        raise RunOperationTransitionError("a blocked verdict may only be followed by terminal")
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
        provider_id=_strict_text(provider_id, field="provider_id", limit=MAX_TEXT_CHARS),
        writer_attempt=_strict_count(writer_attempt, field="writer_attempt", minimum=1),
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
        provider_id=_strict_text(provider_id, field="provider_id", limit=MAX_TEXT_CHARS),
        turns_used=_strict_count(turns_used, field="turns_used"),
        stop_reason=_strict_text(
            stop_reason, field="stop_reason", limit=MAX_TEXT_CHARS, allow_empty=True
        ),
    )


def mark_completion_proof_recorded(
    state: RunOperationState,
    *,
    proof_ref: str,
    proof_status: str,
    proof_satisfied: bool,
) -> RunOperationState:
    ref = _strict_text(proof_ref, field="proof_ref", limit=MAX_REF_CHARS)
    status = _strict_text(proof_status, field="proof_status", limit=MAX_TEXT_CHARS)
    if not isinstance(proof_satisfied, bool):
        # 1 == True and 0 == False in Python; the flag is a bool or refused.
        raise RunOperationTransitionError("proof_satisfied must be a bool")
    if not _COMPLETION_PROOF_REF_RE.match(ref):
        raise RunOperationTransitionError("proof_ref must be a completion_proof:<16 hex> ref")
    if status not in _RECORDED_PROOF_STATUSES:
        raise RunOperationTransitionError("proof_status is not a recorded status")
    if proof_satisfied != (status == "complete"):
        raise RunOperationTransitionError("proof_satisfied must match the recorded status")
    return _transition(
        state,
        PHASE_COMPLETION_PROOF_RECORDED,
        completion_proof_ref=ref,
        completion_proof_status=status,
        completion_proof_satisfied=proof_satisfied,
    )


def mark_repair_context_admitted(
    state: RunOperationState,
    *,
    context_ref: str,
) -> RunOperationState:
    if not _repair_candidate_proof(
        state.completion_proof_ref, state.completion_proof_status, state.completion_proof_satisfied
    ):
        raise RunOperationTransitionError(
            "only an unsatisfied failed proof admits a repair context"
        )
    digest = _strict_text(context_ref, field="context_ref", limit=MAX_REF_CHARS)
    if not _SHA256_REF_RE.match(digest):
        raise RunOperationTransitionError("context_ref must be a sha256:<64 hex> digest ref")
    return _transition(state, PHASE_REPAIR_CONTEXT_ADMITTED, repair_context_ref=digest)


def mark_repair_running(state: RunOperationState, *, provider_id: str) -> RunOperationState:
    if state.repair_rounds >= state.max_repair_rounds:
        raise RunOperationTransitionError("repair round budget is exhausted")
    return _transition(
        state,
        PHASE_REPAIR_RUNNING,
        provider_id=_strict_text(provider_id, field="provider_id", limit=MAX_TEXT_CHARS),
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
    verdict = _strict_text(
        blocked_reason, field="blocked_reason", limit=MAX_TEXT_CHARS, allow_empty=True
    )
    if verdict and not _blocked_verdict_supported(
        state.completion_proof_ref, state.completion_proof_status, state.completion_proof_satisfied
    ):
        raise RunOperationTransitionError(
            "a blocked verdict requires an unsatisfied failed/blocked proof"
        )
    return _transition(
        state,
        PHASE_REPAIR_SETTLED,
        provider_id=_strict_text(provider_id, field="provider_id", limit=MAX_TEXT_CHARS),
        stop_reason=_strict_text(
            stop_reason, field="stop_reason", limit=MAX_TEXT_CHARS, allow_empty=True
        ),
        blocked_reason=verdict,
        turns_used=(
            state.turns_used if turns_used is None else _strict_count(turns_used, field="turns_used")
        ),
    )


def mark_completion_blocked(
    state: RunOperationState,
    *,
    reason: str,
) -> RunOperationState:
    """Record the enforcement decision on the already-recorded proof.

    The decision point runs after ``completion_proof_recorded``; it updates
    the verdict field without inventing a ninth phase, and only a proof
    that failed the run can carry the verdict.
    """

    if state.phase != PHASE_COMPLETION_PROOF_RECORDED:
        raise RunOperationTransitionError(
            f"completion verdict requires phase {PHASE_COMPLETION_PROOF_RECORDED}, not {state.phase}"
        )
    if not _blocked_verdict_supported(
        state.completion_proof_ref, state.completion_proof_status, state.completion_proof_satisfied
    ):
        raise RunOperationTransitionError(
            "a blocked verdict requires an unsatisfied failed/blocked proof"
        )
    return replace(
        state,
        blocked_reason=_strict_text(reason, field="reason", limit=MAX_TEXT_CHARS),
        updated_at=_now(),
    )


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
    verdict = _strict_text(
        state.blocked_reason if blocked_reason is None else blocked_reason,
        field="blocked_reason",
        limit=MAX_TEXT_CHARS,
        allow_empty=True,
    )
    if verdict and not _blocked_verdict_supported(
        state.completion_proof_ref, state.completion_proof_status, state.completion_proof_satisfied
    ):
        raise RunOperationTransitionError(
            "a blocked verdict requires an unsatisfied failed/blocked proof"
        )
    terminal = RunOperationTerminal(
        stop_reason=_strict_text(
            stop_reason, field="stop_reason", limit=MAX_TEXT_CHARS, allow_empty=True
        ),
        summary_chars=_strict_count(summary_chars, field="summary_chars"),
        turns=_strict_count(turns, field="turns"),
        max_turns=_strict_count(max_turns, field="max_turns"),
        provider=_strict_text(provider, field="provider", limit=MAX_TEXT_CHARS, allow_empty=True),
        blocked_reason=verdict,
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
        project: object = "",
        provider_id: str,
        turn_budget: int,
        max_repair_rounds: int,
    ) -> RunOperationState | None:
        """Open the run's register; refuse to clobber anything on disk.

        Every argument is validated, never clipped or coerced: a
        non-canonical session/run id, provider id, or budget is refused
        without writing -- a register written under a trimmed id could never
        be found again by commits keyed on the original id. The exists check
        and the write share the commit file lock, so two concurrent starts
        produce exactly one register, and a corrupted leftover file is never
        silently overwritten.
        """

        canonical_session = _canonical_id(session_id)
        canonical_run = _canonical_id(run_id)
        if not canonical_session or not canonical_run:
            return None
        try:
            provider = _strict_text(provider_id, field="provider_id", limit=MAX_TEXT_CHARS)
            turn_budget = _strict_count(turn_budget, field="turn_budget")
            max_repair_rounds = _strict_count(max_repair_rounds, field="max_repair_rounds")
        except RunOperationTransitionError:
            # start()'s refusal contract is None, like identity: nothing was
            # written, so there is no half-open register to recover from.
            return None
        path = self.path_for(canonical_session, canonical_run)
        with with_file_lock(path):
            if path.exists():
                return None
            now = _now()
            state = RunOperationState(
                session_id=canonical_session,
                run_id=canonical_run,
                project_ref=_project_ref(project),
                provider_id=provider,
                turn_budget=turn_budget,
                max_repair_rounds=max_repair_rounds,
                phase=PHASE_ACCEPTED,
                started_at=now,
                updated_at=now,
            )
            try:
                write_json_atomic(path, state.to_payload(), max_bytes=MAX_OPERATION_BYTES)
            except (OSError, ValueError):
                return None
        return state

    def load(self, session_id: str, run_id: str) -> RunOperationState | None:
        payload = read_json(self.path_for(session_id, run_id), max_bytes=MAX_OPERATION_BYTES)
        if payload is None:
            return None
        state = RunOperationState.from_payload(payload)
        if state is None or state.session_id != str(session_id or "") or state.run_id != str(
            run_id or ""
        ):
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
            # The writer is held to the reader's bar: the candidate is
            # re-derived through the canonical schema before it may touch
            # the disk, and the register's identity may not move. A
            # transition that smuggles in a fact the reader would reject
            # fails the commit instead of poisoning the register.
            canonical = RunOperationState.from_payload(next_state.to_payload())
            if (
                canonical is None
                or canonical != next_state
                or canonical.session_id != current.session_id
                or canonical.run_id != current.run_id
            ):
                raise RunOperationTransitionError(
                    "commit refused: the next state is not canonical schema v1 for this register"
                )
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
    "INTERRUPTED_FINISHING",
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
