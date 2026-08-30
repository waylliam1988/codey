"""Runtime-native operation phase effects.

The session log is the durable fact source. This module projects the latest
bounded run phase from append-only runtime effects so completion/repair
progress no longer needs a parallel JSON register.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable

from codey.runtime.outcome import OperationOutcome
from codey.runtime.session_log import RuntimeSessionLog
from codey.storage.local_store import project_key, session_key

SCHEMA_VERSION = 1
KIND = "runtime_operation_state"

PHASE_ACCEPTED = "accepted"
PHASE_WRITER_RUNNING = "writer_running"
PHASE_WRITER_SETTLED = "writer_settled"
PHASE_COMPLETION_PROOF_RECORDED = "completion_proof_recorded"
PHASE_REPAIR_CONTEXT_ADMITTED = "repair_context_admitted"
PHASE_REPAIR_RUNNING = "repair_running"
PHASE_REPAIR_SETTLED = "repair_settled"
PHASE_TERMINAL = "terminal"

PHASES = frozenset({
    PHASE_ACCEPTED,
    PHASE_WRITER_RUNNING,
    PHASE_WRITER_SETTLED,
    PHASE_COMPLETION_PROOF_RECORDED,
    PHASE_REPAIR_CONTEXT_ADMITTED,
    PHASE_REPAIR_RUNNING,
    PHASE_REPAIR_SETTLED,
    PHASE_TERMINAL,
})

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PHASE_ACCEPTED: frozenset({PHASE_WRITER_RUNNING, PHASE_TERMINAL}),
    PHASE_WRITER_RUNNING: frozenset({PHASE_WRITER_SETTLED, PHASE_TERMINAL}),
    PHASE_WRITER_SETTLED: frozenset({PHASE_COMPLETION_PROOF_RECORDED, PHASE_TERMINAL}),
    PHASE_COMPLETION_PROOF_RECORDED: frozenset({
        PHASE_REPAIR_CONTEXT_ADMITTED,
        PHASE_TERMINAL,
    }),
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

_PRE_REPAIR_PHASES = frozenset(
    {PHASE_ACCEPTED, PHASE_WRITER_RUNNING, PHASE_WRITER_SETTLED}
)
_REPAIR_PHASES = frozenset(
    {PHASE_REPAIR_CONTEXT_ADMITTED, PHASE_REPAIR_RUNNING, PHASE_REPAIR_SETTLED}
)
_REPAIR_EXECUTING_PHASES = frozenset({PHASE_REPAIR_RUNNING, PHASE_REPAIR_SETTLED})
_POST_PROOF_PHASES = frozenset({PHASE_COMPLETION_PROOF_RECORDED}) | _REPAIR_PHASES
_VERDICT_PHASES = frozenset(
    {PHASE_COMPLETION_PROOF_RECORDED, PHASE_REPAIR_SETTLED, PHASE_TERMINAL}
)

_KNOWN_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "session_id",
        "run_id",
        "operation_id",
        "lane",
        "project_ref",
        "provider_id",
        "turn_budget",
        "max_repair_rounds",
        "phase",
        "started_at",
        "updated_at",
        "task_kind",
        "writer_attempt",
        "turns_used",
        "stop_reason",
        "completion_proof_ref",
        "completion_proof_status",
        "completion_proof_satisfied",
        "repair_rounds",
        "repair_context_ref",
        "blocked_reason",
        "terminal",
    }
)
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
_COMPLETION_PROOF_REF_RE = re.compile(r"^completion_proof:[0-9a-f]{16}$")
_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RECORDED_PROOF_STATUSES = frozenset(
    {"complete", "complete_with_limitations", "failed", "blocked"}
)
_BLOCKABLE_PROOF_STATUSES = frozenset({"failed", "blocked"})
_REPAIR_SOURCE_PROOF_STATUSES = frozenset({"failed"})


class RuntimeOperationTransitionError(Exception):
    """A runtime phase transition violated the closed transition table."""


def _valid_project_ref(ref: str) -> bool:
    return not ref or _PROJECT_REF_RE.match(ref) is not None


def _proof_facts_claimed(
    proof_ref: str,
    proof_status: str,
    satisfied: object,
) -> bool:
    return bool(proof_ref) or bool(proof_status) or satisfied is not None


def _proof_facts_complete(
    proof_ref: str,
    proof_status: str,
    satisfied: object,
) -> bool:
    return bool(proof_ref) and bool(proof_status) and satisfied is not None


def _repair_facts_claimed(repair_rounds: int, repair_context_ref: str) -> bool:
    return repair_rounds > 0 or bool(repair_context_ref)


def _blocked_verdict_facts_supported(
    proof_ref: str,
    proof_status: str,
    satisfied: object,
) -> bool:
    return (
        _proof_facts_complete(proof_ref, proof_status, satisfied)
        and proof_status in _BLOCKABLE_PROOF_STATUSES
        and satisfied is False
    )


@dataclass(frozen=True)
class RuntimeOperationTerminal:
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
    def from_payload(cls, payload: object) -> "RuntimeOperationTerminal | None":
        if not isinstance(payload, dict):
            return None
        try:
            if set(payload) - _KNOWN_TERMINAL_KEYS:
                raise RuntimeOperationTransitionError("terminal payload carries unknown keys")
            return cls(
                stop_reason=_text(payload.get("stop_reason"), "stop_reason", allow_empty=True),
                summary_chars=_count(payload.get("summary_chars"), "summary_chars"),
                turns=_count(payload.get("turns"), "turns"),
                max_turns=_count(payload.get("max_turns"), "max_turns"),
                provider=_text(payload.get("provider"), "provider", allow_empty=True),
                blocked_reason=_text(payload.get("blocked_reason"), "blocked_reason", allow_empty=True),
                finished_at=_text(payload.get("finished_at"), "finished_at", limit=40),
            )
        except RuntimeOperationTransitionError:
            return None

    def identity(self) -> "RuntimeOperationTerminal":
        return replace(self, finished_at="")


@dataclass(frozen=True)
class RuntimeOperationState:
    session_id: str
    run_id: str
    operation_id: str
    lane: str
    project_ref: str
    provider_id: str
    turn_budget: int
    max_repair_rounds: int
    phase: str
    started_at: str
    updated_at: str
    task_kind: str = "task"
    writer_attempt: int = 1
    turns_used: int = 0
    stop_reason: str = ""
    completion_proof_ref: str = ""
    completion_proof_status: str = ""
    completion_proof_satisfied: bool | None = None
    repair_rounds: int = 0
    repair_context_ref: str = ""
    blocked_reason: str = ""
    terminal: RuntimeOperationTerminal | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "operation_id": self.operation_id,
            "lane": self.lane,
            "project_ref": self.project_ref,
            "provider_id": self.provider_id,
            "turn_budget": self.turn_budget,
            "max_repair_rounds": self.max_repair_rounds,
            "phase": self.phase,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "task_kind": self.task_kind,
            "writer_attempt": self.writer_attempt,
            "turns_used": self.turns_used,
            "stop_reason": self.stop_reason,
            "completion_proof_ref": self.completion_proof_ref,
            "completion_proof_status": self.completion_proof_status,
            "repair_rounds": self.repair_rounds,
            "repair_context_ref": self.repair_context_ref,
            "blocked_reason": self.blocked_reason,
        }
        if self.completion_proof_satisfied is not None:
            payload["completion_proof_satisfied"] = self.completion_proof_satisfied
        if self.terminal is not None:
            payload["terminal"] = self.terminal.to_payload()
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> "RuntimeOperationState | None":
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != SCHEMA_VERSION or payload.get("kind") != KIND:
            return None
        try:
            if set(payload) - _KNOWN_PAYLOAD_KEYS:
                raise RuntimeOperationTransitionError("runtime operation state carries unknown keys")
            session_id = _text(payload.get("session_id"), "session_id", limit=MAX_ID_CHARS)
            run_id = _text(payload.get("run_id"), "run_id", limit=MAX_ID_CHARS)
            operation_id = _text(payload.get("operation_id"), "operation_id", limit=MAX_ID_CHARS)
            lane = _text(payload.get("lane"), "lane", limit=MAX_ID_CHARS)
            phase = _text(payload.get("phase"), "phase", limit=40)
            if phase not in PHASES:
                raise RuntimeOperationTransitionError("unknown phase")
            turn_budget = _count(payload.get("turn_budget"), "turn_budget")
            max_repair_rounds = _count(payload.get("max_repair_rounds"), "max_repair_rounds")
            turns_used = _count(payload.get("turns_used"), "turns_used", maximum=turn_budget)
            repair_rounds = _count(
                payload.get("repair_rounds"),
                "repair_rounds",
                maximum=max_repair_rounds,
            )
            if "completion_proof_satisfied" in payload:
                satisfied = payload["completion_proof_satisfied"]
                if not isinstance(satisfied, bool):
                    raise RuntimeOperationTransitionError("completion_proof_satisfied must be bool")
            else:
                satisfied = None
            project_ref = _text(
                payload.get("project_ref"),
                "project_ref",
                limit=MAX_PROJECT_REF_CHARS,
                allow_empty=True,
            )
            if not _valid_project_ref(project_ref):
                raise RuntimeOperationTransitionError("project_ref must be project ref")
            writer_attempt = _count(payload.get("writer_attempt"), "writer_attempt", minimum=1)
            stop_reason = _text(payload.get("stop_reason"), "stop_reason", allow_empty=True)
            proof_ref = _text(
                payload.get("completion_proof_ref"),
                "completion_proof_ref",
                limit=MAX_REF_CHARS,
                allow_empty=True,
            )
            proof_status = _text(
                payload.get("completion_proof_status"),
                "completion_proof_status",
                allow_empty=True,
            )
            if proof_ref and not _COMPLETION_PROOF_REF_RE.match(proof_ref):
                raise RuntimeOperationTransitionError("completion_proof_ref must be completion proof ref")
            if proof_status and proof_status not in _RECORDED_PROOF_STATUSES:
                raise RuntimeOperationTransitionError("unknown proof status")
            repair_context_ref = _text(
                payload.get("repair_context_ref"),
                "repair_context_ref",
                limit=MAX_REF_CHARS,
                allow_empty=True,
            )
            if repair_context_ref and not _SHA256_REF_RE.match(repair_context_ref):
                raise RuntimeOperationTransitionError("repair_context_ref must be sha256 digest")
            if phase in _REPAIR_PHASES:
                if not repair_context_ref:
                    raise RuntimeOperationTransitionError("repair phase requires context ref")
                if proof_status != "failed":
                    raise RuntimeOperationTransitionError("repair phase requires failed proof")
            if phase in _REPAIR_EXECUTING_PHASES and repair_rounds < 1:
                raise RuntimeOperationTransitionError("repair phase requires committed round")
            if phase in _PRE_REPAIR_PHASES:
                if _repair_facts_claimed(repair_rounds, repair_context_ref):
                    raise RuntimeOperationTransitionError("pre-repair phase cannot carry repair facts")
                if _proof_facts_claimed(proof_ref, proof_status, satisfied):
                    raise RuntimeOperationTransitionError("pre-repair phase cannot carry proof facts")
            elif phase in _POST_PROOF_PHASES:
                if not _proof_facts_complete(proof_ref, proof_status, satisfied):
                    raise RuntimeOperationTransitionError("post-proof phase requires proof facts")
                if satisfied != (proof_status == "complete"):
                    raise RuntimeOperationTransitionError("proof_satisfied must match proof status")
                if phase == PHASE_COMPLETION_PROOF_RECORDED and _repair_facts_claimed(
                    repair_rounds,
                    repair_context_ref,
                ):
                    if not repair_context_ref or repair_rounds < 1:
                        raise RuntimeOperationTransitionError("re-proof carries partial repair record")
            elif phase == PHASE_TERMINAL:
                proof_complete = _proof_facts_complete(proof_ref, proof_status, satisfied)
                if _proof_facts_claimed(proof_ref, proof_status, satisfied) and not proof_complete:
                    raise RuntimeOperationTransitionError("terminal carries incomplete proof facts")
                if _repair_facts_claimed(repair_rounds, repair_context_ref) and not proof_complete:
                    raise RuntimeOperationTransitionError("terminal carries repair facts without proof")
                if repair_rounds > 0 and not repair_context_ref:
                    raise RuntimeOperationTransitionError("terminal carries rounds without context")
                if repair_context_ref and repair_rounds == 0 and proof_status != "failed":
                    raise RuntimeOperationTransitionError("terminal admitted context requires failed proof")
                if proof_complete and satisfied != (proof_status == "complete"):
                    raise RuntimeOperationTransitionError("terminal proof_satisfied must match status")
            blocked_reason = _text(payload.get("blocked_reason"), "blocked_reason", allow_empty=True)
            if blocked_reason and phase not in _VERDICT_PHASES:
                raise RuntimeOperationTransitionError("blocked verdict is not valid for phase")
            if blocked_reason and not _blocked_verdict_facts_supported(
                proof_ref,
                proof_status,
                satisfied,
            ):
                raise RuntimeOperationTransitionError("blocked verdict requires failed or blocked proof")
            if phase == PHASE_ACCEPTED and (writer_attempt != 1 or turns_used or stop_reason):
                raise RuntimeOperationTransitionError("accepted must be fresh")
            if phase == PHASE_WRITER_RUNNING and (turns_used or stop_reason):
                raise RuntimeOperationTransitionError("writer_running cannot carry settled facts")
            terminal = None
            if phase == PHASE_TERMINAL:
                terminal = RuntimeOperationTerminal.from_payload(payload.get("terminal"))
                if terminal is None:
                    raise RuntimeOperationTransitionError("terminal phase requires terminal payload")
                if terminal.max_turns != turn_budget:
                    raise RuntimeOperationTransitionError("terminal max_turns must match budget")
                if terminal.turns > terminal.max_turns:
                    raise RuntimeOperationTransitionError("terminal turns exceeds max_turns")
                if blocked_reason != terminal.blocked_reason:
                    raise RuntimeOperationTransitionError("terminal blocked reason mismatch")
            elif "terminal" in payload:
                raise RuntimeOperationTransitionError("non-terminal must not carry terminal")
            return cls(
                session_id=session_id,
                run_id=run_id,
                operation_id=operation_id,
                lane=lane,
                project_ref=project_ref,
                provider_id=_text(payload.get("provider_id"), "provider_id"),
                turn_budget=turn_budget,
                max_repair_rounds=max_repair_rounds,
                phase=phase,
                started_at=_text(payload.get("started_at"), "started_at", limit=40),
                updated_at=_text(payload.get("updated_at"), "updated_at", limit=40),
                task_kind=_text(payload.get("task_kind"), "task_kind"),
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
        except RuntimeOperationTransitionError:
            return None


class RuntimeOperationStore:
    """Append-only runtime phase projection for task progress."""

    def __init__(self, session_log: RuntimeSessionLog) -> None:
        self.session_log = session_log

    def start(
        self,
        *,
        session_id: str,
        run_id: str,
        project: object = "",
        provider_id: str,
        turn_budget: int,
        max_repair_rounds: int,
        task_kind: str = "task",
    ) -> RuntimeOperationState | None:
        try:
            session = _canonical_id(session_id)
            run = _canonical_id(run_id)
            provider = _text(provider_id, "provider_id")
            budget = _count(turn_budget, "turn_budget")
            repair_budget = _count(max_repair_rounds, "max_repair_rounds")
            now = _now()
            state = RuntimeOperationState(
                session_id=session,
                run_id=run,
                operation_id=operation_id_for_run(run),
                lane=lane_for_run(run),
                project_ref=_project_ref(project),
                provider_id=provider,
                turn_budget=budget,
                max_repair_rounds=repair_budget,
                phase=PHASE_ACCEPTED,
                started_at=now,
                updated_at=now,
                task_kind=_text(task_kind, "task_kind"),
            )
            self.session_log.append_many(
                session,
                (
                    {
                        "lane": state.lane,
                        "operation_id": state.operation_id,
                        "kind": "operation_started",
                        "payload": {"operation_kind": state.task_kind},
                    },
                    _phase_entry(state),
                ),
            )
        except Exception:
            return None
        return state

    def load(self, session_id: str, run_id: str) -> RuntimeOperationState | None:
        operation_id = operation_id_for_run(str(run_id or ""))
        lane = lane_for_run(str(run_id or ""))
        latest: RuntimeOperationState | None = None
        try:
            entries = self.session_log.read(session_id)
        except Exception:
            return None
        for entry in entries:
            if entry.operation_id != operation_id or entry.kind != "operation_effect":
                continue
            if entry.payload.get("effect_kind") != "run_phase":
                continue
            state = RuntimeOperationState.from_payload(entry.payload.get("state"))
            if (
                state is not None
                and state.session_id == session_id
                and state.run_id == run_id
                and state.operation_id == operation_id
                and state.lane == lane
            ):
                latest = state
        return latest

    def commit(
        self,
        session_id: str,
        run_id: str,
        transition: Callable[[RuntimeOperationState], RuntimeOperationState],
    ) -> RuntimeOperationState | None:
        current = self.load(session_id, run_id)
        if current is None or current.phase == PHASE_TERMINAL:
            return current
        try:
            next_state = transition(current)
            canonical = RuntimeOperationState.from_payload(next_state.to_payload())
            if (
                canonical != next_state
                or canonical.session_id != current.session_id
                or canonical.run_id != current.run_id
                or canonical.operation_id != current.operation_id
                or canonical.lane != current.lane
            ):
                raise RuntimeOperationTransitionError("non-canonical runtime phase")
            entries = [_phase_entry(next_state)]
            if next_state.phase == PHASE_TERMINAL:
                entries.append({
                    "lane": next_state.lane,
                    "operation_id": next_state.operation_id,
                    "kind": "operation_settled",
                    "payload": _outcome_for_terminal(next_state).to_payload(),
                })
            self.session_log.append_many(session_id, entries)
        except RuntimeOperationTransitionError:
            raise
        except Exception:
            return None
        return next_state

    def delete_session(self, session_id: str) -> None:
        try:
            path = self.session_log.path_for(session_id)
            path.unlink()
        except FileNotFoundError:
            return
        except OSError:
            return


def operation_id_for_run(run_id: str) -> str:
    return "task:" + hashlib.sha256(str(run_id or "").encode("utf-8")).hexdigest()[:24]


def lane_for_run(run_id: str) -> str:
    return "run:" + session_key(str(run_id or ""))[:24]


def operation_progress_text(state: RuntimeOperationState | None) -> str:
    if state is None or state.phase == PHASE_TERMINAL:
        return ""
    if state.phase in (PHASE_ACCEPTED, PHASE_WRITER_RUNNING):
        return INTERRUPTED_WRITING
    if state.phase in (PHASE_REPAIR_CONTEXT_ADMITTED, PHASE_REPAIR_RUNNING):
        return INTERRUPTED_REPAIR
    if state.phase == PHASE_COMPLETION_PROOF_RECORDED and state.completion_proof_satisfied:
        return INTERRUPTED_FINISHING
    return INTERRUPTED_COMPLETION_CHECK


def mark_writer_running(
    state: RuntimeOperationState,
    *,
    provider_id: str,
    writer_attempt: int = 1,
) -> RuntimeOperationState:
    return _transition(
        state,
        PHASE_WRITER_RUNNING,
        provider_id=_text(provider_id, "provider_id"),
        writer_attempt=_count(writer_attempt, "writer_attempt", minimum=1),
    )


def mark_writer_settled(
    state: RuntimeOperationState,
    *,
    provider_id: str,
    turns_used: int,
    stop_reason: str,
) -> RuntimeOperationState:
    return _transition(
        state,
        PHASE_WRITER_SETTLED,
        provider_id=_text(provider_id, "provider_id"),
        turns_used=_count(turns_used, "turns_used", maximum=state.turn_budget),
        stop_reason=_text(stop_reason, "stop_reason", allow_empty=True),
    )


def mark_completion_proof_recorded(
    state: RuntimeOperationState,
    *,
    proof_ref: str,
    proof_status: str,
    proof_satisfied: bool,
) -> RuntimeOperationState:
    ref = _text(proof_ref, "proof_ref", limit=MAX_REF_CHARS)
    status = _text(proof_status, "proof_status")
    if not isinstance(proof_satisfied, bool):
        raise RuntimeOperationTransitionError("proof_satisfied must be a bool")
    if not _COMPLETION_PROOF_REF_RE.match(ref):
        raise RuntimeOperationTransitionError("proof_ref must be completion proof ref")
    if status not in _RECORDED_PROOF_STATUSES:
        raise RuntimeOperationTransitionError("unknown proof status")
    if proof_satisfied != (status == "complete"):
        raise RuntimeOperationTransitionError("proof_satisfied must match proof status")
    return _transition(
        state,
        PHASE_COMPLETION_PROOF_RECORDED,
        completion_proof_ref=ref,
        completion_proof_status=status,
        completion_proof_satisfied=proof_satisfied,
    )


def mark_repair_context_admitted(
    state: RuntimeOperationState,
    *,
    context_ref: str,
) -> RuntimeOperationState:
    if not _repair_candidate_proof(state):
        raise RuntimeOperationTransitionError("repair context requires a failed proof")
    digest = _text(context_ref, "context_ref", limit=MAX_REF_CHARS)
    if not _SHA256_REF_RE.match(digest):
        raise RuntimeOperationTransitionError("context_ref must be sha256 digest")
    return _transition(state, PHASE_REPAIR_CONTEXT_ADMITTED, repair_context_ref=digest)


def mark_repair_running(
    state: RuntimeOperationState,
    *,
    provider_id: str,
) -> RuntimeOperationState:
    if state.repair_rounds >= state.max_repair_rounds:
        raise RuntimeOperationTransitionError("repair round budget exhausted")
    return _transition(
        state,
        PHASE_REPAIR_RUNNING,
        provider_id=_text(provider_id, "provider_id"),
        repair_rounds=state.repair_rounds + 1,
    )


def mark_repair_settled(
    state: RuntimeOperationState,
    *,
    provider_id: str,
    stop_reason: str,
    blocked_reason: str = "",
    turns_used: int | None = None,
) -> RuntimeOperationState:
    verdict = _text(blocked_reason, "blocked_reason", allow_empty=True)
    if verdict and not _blocked_verdict_supported(state):
        raise RuntimeOperationTransitionError("blocked verdict requires failed proof")
    return _transition(
        state,
        PHASE_REPAIR_SETTLED,
        provider_id=_text(provider_id, "provider_id"),
        stop_reason=_text(stop_reason, "stop_reason", allow_empty=True),
        blocked_reason=verdict,
        turns_used=(
            state.turns_used
            if turns_used is None
            else _count(turns_used, "turns_used", maximum=state.turn_budget)
        ),
    )


def mark_completion_blocked(
    state: RuntimeOperationState,
    *,
    reason: str,
) -> RuntimeOperationState:
    if state.phase != PHASE_COMPLETION_PROOF_RECORDED:
        raise RuntimeOperationTransitionError("completion verdict requires recorded proof")
    if not _blocked_verdict_supported(state):
        raise RuntimeOperationTransitionError("blocked verdict requires failed proof")
    return replace(state, blocked_reason=_text(reason, "reason"), updated_at=_now())


def mark_terminal(
    state: RuntimeOperationState,
    *,
    stop_reason: str,
    summary_chars: int,
    turns: int,
    max_turns: int,
    provider: str,
    blocked_reason: str | None = None,
) -> RuntimeOperationState:
    verdict = _text(
        state.blocked_reason if blocked_reason is None else blocked_reason,
        "blocked_reason",
        allow_empty=True,
    )
    if verdict and not _blocked_verdict_supported(state):
        raise RuntimeOperationTransitionError("blocked verdict requires failed proof")
    terminal = RuntimeOperationTerminal(
        stop_reason=_text(stop_reason, "stop_reason", allow_empty=True),
        summary_chars=_count(summary_chars, "summary_chars"),
        turns=_count(turns, "turns", maximum=state.turn_budget),
        max_turns=_count(max_turns, "max_turns"),
        provider=_text(provider, "provider", allow_empty=True),
        blocked_reason=verdict,
        finished_at=_now(),
    )
    if terminal.max_turns != state.turn_budget:
        raise RuntimeOperationTransitionError("terminal max_turns must match budget")
    if state.phase == PHASE_TERMINAL:
        if state.terminal is not None and state.terminal.identity() == terminal.identity():
            return state
        raise RuntimeOperationTransitionError("terminal state is immutable")
    return _transition(
        state,
        PHASE_TERMINAL,
        terminal=terminal,
        blocked_reason=terminal.blocked_reason,
    )


def _transition(
    state: RuntimeOperationState,
    next_phase: str,
    **updates: object,
) -> RuntimeOperationState:
    if next_phase not in _ALLOWED_TRANSITIONS.get(state.phase, frozenset()):
        raise RuntimeOperationTransitionError(f"illegal transition {state.phase} -> {next_phase}")
    if state.blocked_reason and next_phase != PHASE_TERMINAL:
        raise RuntimeOperationTransitionError("blocked verdict may only finish")
    return replace(state, phase=next_phase, updated_at=_now(), **updates)  # type: ignore[arg-type]


def _phase_entry(state: RuntimeOperationState) -> dict[str, object]:
    return {
        "lane": state.lane,
        "operation_id": state.operation_id,
        "kind": "operation_effect",
        "payload": {
            "effect_kind": "run_phase",
            "ref": f"run_phase:{state.phase}",
            "state": state.to_payload(),
        },
    }


def _outcome_for_terminal(state: RuntimeOperationState) -> OperationOutcome:
    reason = state.terminal.stop_reason if state.terminal is not None else state.stop_reason
    if reason == "stopped":
        return OperationOutcome.aborted(reason="stopped")
    if reason == "error":
        return OperationOutcome.failed(reason="error")
    if state.blocked_reason:
        return OperationOutcome.failed(reason=state.blocked_reason)
    return OperationOutcome.completed(summary=reason)


def _repair_candidate_proof(state: RuntimeOperationState) -> bool:
    return (
        bool(state.completion_proof_ref)
        and state.completion_proof_status in _REPAIR_SOURCE_PROOF_STATUSES
        and state.completion_proof_satisfied is False
    )


def _blocked_verdict_supported(state: RuntimeOperationState) -> bool:
    return (
        bool(state.completion_proof_ref)
        and state.completion_proof_status in _BLOCKABLE_PROOF_STATUSES
        and state.completion_proof_satisfied is False
    )


def _project_ref(project: object) -> str:
    text = str(project or "").strip()
    return f"project:{project_key(text)}" if text else ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_id(value: object) -> str:
    return _text(value, "id", limit=MAX_ID_CHARS)


def _text(
    value: object,
    field: str,
    *,
    limit: int = MAX_TEXT_CHARS,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise RuntimeOperationTransitionError(f"{field} must be string")
    text = value.strip()
    if text != value:
        raise RuntimeOperationTransitionError(f"{field} must be canonical")
    if not text and not allow_empty:
        raise RuntimeOperationTransitionError(f"{field} must not be empty")
    if len(text) > limit:
        raise RuntimeOperationTransitionError(f"{field} exceeds {limit} chars")
    return text


def _count(
    value: object,
    field: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RuntimeOperationTransitionError(f"{field} must be int >= {minimum}")
    if maximum is not None and value > maximum:
        raise RuntimeOperationTransitionError(f"{field} must be at most {maximum}")
    return value


__all__ = [
    "INTERRUPTED_COMPLETION_CHECK",
    "INTERRUPTED_FINISHING",
    "INTERRUPTED_REPAIR",
    "INTERRUPTED_WRITING",
    "KIND",
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
    "RuntimeOperationState",
    "RuntimeOperationStore",
    "RuntimeOperationTerminal",
    "RuntimeOperationTransitionError",
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
