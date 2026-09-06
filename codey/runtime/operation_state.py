"""Durable operation state for Codey's runtime core.

OperationState is a first-class log record. It answers "where may recovery
start, and which next action is legal?" External effects remain in the effect
ledger; UI events and run traces are observation only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Iterable

from codey.runtime.outcome import OperationOutcome, operation_outcome_from_stop_reason
from codey.runtime.session_log import RuntimeLogEntry, RuntimeSessionLog
from codey.runtime.session_projection import RuntimeProjection
from codey.storage.local_store import project_key, session_key

SCHEMA_VERSION = 1
KIND = "runtime_operation_state"

LEAF_ACCEPTED = "accepted"
LEAF_WRITER_RUNNING = "writer_running"
LEAF_PROVIDER_EFFECT_PENDING = "provider_effect_pending"
LEAF_TOOL_EFFECT_PENDING = "tool_effect_pending"
LEAF_TOOL_DELIVERY_PENDING = "tool_delivery_pending"
LEAF_WRITER_SETTLED = "writer_settled"
LEAF_COMPLETION_PROOF_RECORDED = "completion_proof_recorded"
LEAF_REPAIR_CONTEXT_ADMITTED = "repair_context_admitted"
LEAF_REPAIR_RUNNING = "repair_running"
LEAF_REPAIR_SETTLED = "repair_settled"
LEAF_TERMINAL = "terminal"

DRIVER_WRITER = "writer"
DRIVER_REPAIR = "repair"
DRIVERS = frozenset({DRIVER_WRITER, DRIVER_REPAIR})

LEAVES = frozenset({
    LEAF_ACCEPTED,
    LEAF_WRITER_RUNNING,
    LEAF_PROVIDER_EFFECT_PENDING,
    LEAF_TOOL_EFFECT_PENDING,
    LEAF_TOOL_DELIVERY_PENDING,
    LEAF_WRITER_SETTLED,
    LEAF_COMPLETION_PROOF_RECORDED,
    LEAF_REPAIR_CONTEXT_ADMITTED,
    LEAF_REPAIR_RUNNING,
    LEAF_REPAIR_SETTLED,
    LEAF_TERMINAL,
})

_PENDING_LEAVES = frozenset({
    LEAF_PROVIDER_EFFECT_PENDING,
    LEAF_TOOL_EFFECT_PENDING,
    LEAF_TOOL_DELIVERY_PENDING,
})

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    LEAF_ACCEPTED: frozenset({LEAF_WRITER_RUNNING, LEAF_TERMINAL}),
    LEAF_WRITER_RUNNING: frozenset({
        LEAF_PROVIDER_EFFECT_PENDING,
        LEAF_TOOL_EFFECT_PENDING,
        LEAF_TOOL_DELIVERY_PENDING,
        LEAF_WRITER_SETTLED,
        LEAF_TERMINAL,
    }),
    LEAF_PROVIDER_EFFECT_PENDING: frozenset({
        LEAF_WRITER_RUNNING,
        LEAF_REPAIR_RUNNING,
        LEAF_TERMINAL,
    }),
    LEAF_TOOL_EFFECT_PENDING: frozenset({
        LEAF_TOOL_EFFECT_PENDING,
        LEAF_TOOL_DELIVERY_PENDING,
        LEAF_TERMINAL,
    }),
    LEAF_TOOL_DELIVERY_PENDING: frozenset({
        LEAF_PROVIDER_EFFECT_PENDING,
        LEAF_WRITER_RUNNING,
        LEAF_REPAIR_RUNNING,
        LEAF_TERMINAL,
    }),
    LEAF_WRITER_SETTLED: frozenset({LEAF_COMPLETION_PROOF_RECORDED, LEAF_TERMINAL}),
    LEAF_COMPLETION_PROOF_RECORDED: frozenset({
        LEAF_REPAIR_CONTEXT_ADMITTED,
        LEAF_TERMINAL,
    }),
    LEAF_REPAIR_CONTEXT_ADMITTED: frozenset({LEAF_REPAIR_RUNNING, LEAF_TERMINAL}),
    LEAF_REPAIR_RUNNING: frozenset({
        LEAF_PROVIDER_EFFECT_PENDING,
        LEAF_TOOL_EFFECT_PENDING,
        LEAF_TOOL_DELIVERY_PENDING,
        LEAF_REPAIR_SETTLED,
        LEAF_TERMINAL,
    }),
    LEAF_REPAIR_SETTLED: frozenset({LEAF_COMPLETION_PROOF_RECORDED, LEAF_TERMINAL}),
    LEAF_TERMINAL: frozenset({LEAF_TERMINAL}),
}

MAX_TEXT_CHARS = 80
MAX_REF_CHARS = 160
MAX_PROJECT_REF_CHARS = 240
MAX_ID_CHARS = 200
MAX_PENDING_EFFECT_IDS = 64

INTERRUPTED_WRITING = "Writing was interrupted"
INTERRUPTED_COMPLETION_CHECK = "Completion check was interrupted"
INTERRUPTED_FINISHING = "Finishing was interrupted"
INTERRUPTED_REPAIR = "Stopped during repair"

_PRE_REPAIR_LEAVES = frozenset({
    LEAF_ACCEPTED,
    LEAF_WRITER_RUNNING,
    LEAF_WRITER_SETTLED,
})
_REPAIR_STRUCTURAL_LEAVES = frozenset({
    LEAF_REPAIR_CONTEXT_ADMITTED,
    LEAF_REPAIR_RUNNING,
    LEAF_REPAIR_SETTLED,
})
_POST_PROOF_BASE_LEAVES = frozenset({LEAF_COMPLETION_PROOF_RECORDED}) | _REPAIR_STRUCTURAL_LEAVES
_VERDICT_LEAVES = frozenset({
    LEAF_COMPLETION_PROOF_RECORDED,
    LEAF_REPAIR_SETTLED,
    LEAF_TERMINAL,
})

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
        "leaf",
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
        "driver",
        "pending_effect_category",
        "pending_effect_ids",
        "pending_delivery_batch_id",
        "turn",
        "tool_index",
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
    """An operation state transition violated the closed leaf table."""


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
    leaf: str
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
    driver: str = ""
    pending_effect_category: str = ""
    pending_effect_ids: tuple[str, ...] = ()
    pending_delivery_batch_id: str = ""
    turn: int = 0
    tool_index: int = 0
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
            "leaf": self.leaf,
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
            "driver": self.driver,
            "pending_effect_category": self.pending_effect_category,
            "pending_effect_ids": list(self.pending_effect_ids),
            "pending_delivery_batch_id": self.pending_delivery_batch_id,
            "turn": self.turn,
            "tool_index": self.tool_index,
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
            leaf = _text(payload.get("leaf"), "leaf", limit=48)
            if leaf not in LEAVES:
                raise RuntimeOperationTransitionError("unknown operation leaf")

            turn_budget = _count(payload.get("turn_budget"), "turn_budget")
            max_repair_rounds = _count(payload.get("max_repair_rounds"), "max_repair_rounds")
            turns_used = _count(payload.get("turns_used"), "turns_used", maximum=turn_budget)
            repair_rounds = _count(
                payload.get("repair_rounds"),
                "repair_rounds",
                maximum=max_repair_rounds,
            )
            turn = _count(payload.get("turn"), "turn")
            tool_index = _count(payload.get("tool_index"), "tool_index")
            pending_effect_ids = _pending_effect_ids(payload.get("pending_effect_ids", ()))

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
            driver = _text(payload.get("driver"), "driver", allow_empty=True)
            pending_category = _text(
                payload.get("pending_effect_category"),
                "pending_effect_category",
                allow_empty=True,
            )
            pending_delivery_batch_id = _text(
                payload.get("pending_delivery_batch_id"),
                "pending_delivery_batch_id",
                limit=MAX_REF_CHARS,
                allow_empty=True,
            )
            _validate_pending_leaf(
                leaf=leaf,
                driver=driver,
                pending_category=pending_category,
                pending_effect_ids=pending_effect_ids,
                pending_delivery_batch_id=pending_delivery_batch_id,
                turn=turn,
                tool_index=tool_index,
            )

            repair_leaf = _is_repair_leaf(leaf, driver)
            if repair_leaf:
                if not repair_context_ref:
                    raise RuntimeOperationTransitionError("repair leaf requires context ref")
                if proof_status != "failed":
                    raise RuntimeOperationTransitionError("repair leaf requires failed proof")
            if repair_leaf and leaf != LEAF_REPAIR_CONTEXT_ADMITTED and repair_rounds < 1:
                raise RuntimeOperationTransitionError("repair execution requires committed round")

            if leaf in _PRE_REPAIR_LEAVES or _is_writer_pending_leaf(leaf, driver):
                if _repair_facts_claimed(repair_rounds, repair_context_ref):
                    raise RuntimeOperationTransitionError("pre-repair leaf cannot carry repair facts")
                if _proof_facts_claimed(proof_ref, proof_status, satisfied):
                    raise RuntimeOperationTransitionError("pre-repair leaf cannot carry proof facts")
            elif leaf in _POST_PROOF_BASE_LEAVES or repair_leaf:
                if not _proof_facts_complete(proof_ref, proof_status, satisfied):
                    raise RuntimeOperationTransitionError("post-proof leaf requires proof facts")
                if satisfied != (proof_status == "complete"):
                    raise RuntimeOperationTransitionError("proof_satisfied must match proof status")
                if leaf == LEAF_COMPLETION_PROOF_RECORDED and _repair_facts_claimed(
                    repair_rounds,
                    repair_context_ref,
                ):
                    if not repair_context_ref or repair_rounds < 1:
                        raise RuntimeOperationTransitionError("re-proof carries partial repair record")
            elif leaf == LEAF_TERMINAL:
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
            if blocked_reason and leaf not in _VERDICT_LEAVES:
                raise RuntimeOperationTransitionError("blocked verdict is not valid for leaf")
            if blocked_reason and not _blocked_verdict_facts_supported(
                proof_ref,
                proof_status,
                satisfied,
            ):
                raise RuntimeOperationTransitionError("blocked verdict requires failed or blocked proof")
            if leaf == LEAF_ACCEPTED and (writer_attempt != 1 or turns_used or stop_reason):
                raise RuntimeOperationTransitionError("accepted must be fresh")
            if leaf == LEAF_WRITER_RUNNING and (turns_used or stop_reason):
                raise RuntimeOperationTransitionError("writer_running cannot carry settled facts")

            terminal = None
            if leaf == LEAF_TERMINAL:
                terminal = RuntimeOperationTerminal.from_payload(payload.get("terminal"))
                if terminal is None:
                    raise RuntimeOperationTransitionError("terminal leaf requires terminal payload")
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
                leaf=leaf,
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
                driver=driver,
                pending_effect_category=pending_category,
                pending_effect_ids=pending_effect_ids,
                pending_delivery_batch_id=pending_delivery_batch_id,
                turn=turn,
                tool_index=tool_index,
                terminal=terminal,
            )
        except RuntimeOperationTransitionError:
            return None


class RuntimeOperationStore:
    """Read projection for one run's durable operation state."""

    def __init__(self, session_log: RuntimeSessionLog) -> None:
        self.session_log = session_log

    def load(self, session_id: str, run_id: str) -> RuntimeOperationState | None:
        try:
            return operation_state_from_entries(
                self.session_log.entries(session_id),
                session_id=session_id,
                run_id=run_id,
            )
        except Exception:
            return None


def operation_id_for_run(run_id: str) -> str:
    return "task:" + hashlib.sha256(str(run_id or "").encode("utf-8")).hexdigest()[:24]


def lane_for_run(run_id: str) -> str:
    return "run:" + session_key(str(run_id or ""))[:24]


def new_operation_state(
    *,
    session_id: str,
    run_id: str,
    project: object = "",
    provider_id: str,
    turn_budget: int,
    max_repair_rounds: int,
    task_kind: str = "task",
) -> RuntimeOperationState:
    session = _canonical_id(session_id)
    run = _canonical_id(run_id)
    now = _now()
    return RuntimeOperationState(
        session_id=session,
        run_id=run,
        operation_id=operation_id_for_run(run),
        lane=lane_for_run(run),
        project_ref=_project_ref(project),
        provider_id=_text(provider_id, "provider_id"),
        turn_budget=_count(turn_budget, "turn_budget"),
        max_repair_rounds=_count(max_repair_rounds, "max_repair_rounds"),
        leaf=LEAF_ACCEPTED,
        started_at=now,
        updated_at=now,
        task_kind=_text(task_kind, "task_kind"),
    )


def operation_state_from_entries(
    entries: Iterable[RuntimeLogEntry],
    *,
    session_id: str,
    run_id: str,
) -> RuntimeOperationState | None:
    operation_id = operation_id_for_run(str(run_id or ""))
    lane = lane_for_run(str(run_id or ""))
    latest: RuntimeOperationState | None = None
    for entry in entries:
        if entry.operation_id != operation_id or entry.kind != "operation_state":
            continue
        state = RuntimeOperationState.from_payload(entry.payload)
        if (
            state is not None
            and state.session_id == session_id
            and state.run_id == run_id
            and state.operation_id == operation_id
            and state.lane == lane
        ):
            latest = state
    return latest


def operation_progress_text(state: RuntimeOperationState | None) -> str:
    if state is None or state.leaf == LEAF_TERMINAL:
        return ""
    if state.leaf in {
        LEAF_ACCEPTED,
        LEAF_WRITER_RUNNING,
        LEAF_PROVIDER_EFFECT_PENDING,
        LEAF_TOOL_EFFECT_PENDING,
        LEAF_TOOL_DELIVERY_PENDING,
    } and not _is_repair_leaf(state.leaf, state.driver):
        return INTERRUPTED_WRITING
    if state.leaf in {
        LEAF_REPAIR_CONTEXT_ADMITTED,
        LEAF_REPAIR_RUNNING,
        LEAF_PROVIDER_EFFECT_PENDING,
        LEAF_TOOL_EFFECT_PENDING,
        LEAF_TOOL_DELIVERY_PENDING,
    } and _is_repair_leaf(state.leaf, state.driver):
        return INTERRUPTED_REPAIR
    if state.leaf == LEAF_COMPLETION_PROOF_RECORDED and state.completion_proof_satisfied:
        return INTERRUPTED_FINISHING
    return INTERRUPTED_COMPLETION_CHECK


def mark_writer_running(
    state: RuntimeOperationState,
    *,
    provider_id: str,
    writer_attempt: int = 1,
) -> RuntimeOperationState:
    provider = _text(provider_id, "provider_id")
    attempt = _count(writer_attempt, "writer_attempt", minimum=1)
    if (
        state.leaf == LEAF_WRITER_RUNNING
        and state.provider_id == provider
        and state.writer_attempt == attempt
    ):
        return state
    return _transition(
        state,
        LEAF_WRITER_RUNNING,
        provider_id=provider,
        writer_attempt=attempt,
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
        LEAF_WRITER_SETTLED,
        provider_id=_text(provider_id, "provider_id"),
        turns_used=_count(turns_used, "turns_used", maximum=state.turn_budget),
        stop_reason=_text(stop_reason, "stop_reason", allow_empty=True),
    )


def mark_provider_effect_pending(
    state: RuntimeOperationState,
    *,
    effect_id: str,
    driver: str,
    provider_id: str,
    turn: int,
    delivery_batch_id: str = "",
) -> RuntimeOperationState:
    if driver not in DRIVERS:
        raise RuntimeOperationTransitionError("provider effect driver must be writer or repair")
    return _transition(
        state,
        LEAF_PROVIDER_EFFECT_PENDING,
        provider_id=_text(provider_id, "provider_id"),
        driver=driver,
        pending_effect_category="provider_send",
        pending_effect_ids=(_text(effect_id, "effect_id", limit=MAX_REF_CHARS),),
        pending_delivery_batch_id=_text(
            delivery_batch_id,
            "delivery_batch_id",
            limit=MAX_REF_CHARS,
            allow_empty=True,
        ),
        turn=_count(turn, "turn"),
        tool_index=0,
    )


def mark_provider_effect_settled(
    state: RuntimeOperationState,
    *,
    effect_id: str,
) -> RuntimeOperationState:
    _require_pending_effect(state, leaf=LEAF_PROVIDER_EFFECT_PENDING, effect_id=effect_id)
    if state.driver == DRIVER_REPAIR:
        return _transition(state, LEAF_REPAIR_RUNNING)
    return _transition(state, LEAF_WRITER_RUNNING)


def mark_tool_effect_pending(
    state: RuntimeOperationState,
    *,
    effect_ids: tuple[str, ...],
    driver: str,
    delivery_batch_id: str,
    turn: int,
    tool_index: int = 0,
) -> RuntimeOperationState:
    if driver not in DRIVERS:
        raise RuntimeOperationTransitionError("tool effect driver must be writer or repair")
    ids = _pending_effect_ids(effect_ids)
    if not ids:
        raise RuntimeOperationTransitionError("tool effect pending requires at least one effect")
    return _transition(
        state,
        LEAF_TOOL_EFFECT_PENDING,
        driver=driver,
        pending_effect_category="tool_call",
        pending_effect_ids=ids,
        pending_delivery_batch_id=_text(delivery_batch_id, "delivery_batch_id", limit=MAX_REF_CHARS),
        turn=_count(turn, "turn"),
        tool_index=_count(tool_index, "tool_index"),
    )


def mark_tool_effect_settled(
    state: RuntimeOperationState,
    *,
    effect_id: str,
) -> RuntimeOperationState:
    _require_pending_effect(state, leaf=LEAF_TOOL_EFFECT_PENDING, effect_id=effect_id)
    remaining = tuple(eid for eid in state.pending_effect_ids if eid != effect_id)
    if remaining:
        return _transition(
            state,
            LEAF_TOOL_EFFECT_PENDING,
            driver=state.driver,
            pending_effect_category="tool_call",
            pending_effect_ids=remaining,
            pending_delivery_batch_id=state.pending_delivery_batch_id,
            turn=state.turn,
            tool_index=state.tool_index,
        )
    return mark_tool_delivery_pending(
        state,
        driver=state.driver,
        delivery_batch_id=state.pending_delivery_batch_id,
        turn=state.turn,
    )


def mark_tool_delivery_pending(
    state: RuntimeOperationState,
    *,
    driver: str,
    delivery_batch_id: str,
    turn: int,
) -> RuntimeOperationState:
    if driver not in DRIVERS:
        raise RuntimeOperationTransitionError("tool delivery driver must be writer or repair")
    return _transition(
        state,
        LEAF_TOOL_DELIVERY_PENDING,
        driver=driver,
        pending_effect_category="",
        pending_effect_ids=(),
        pending_delivery_batch_id=_text(delivery_batch_id, "delivery_batch_id", limit=MAX_REF_CHARS),
        turn=_count(turn, "turn"),
        tool_index=0,
    )


def mark_tool_delivery_settled(state: RuntimeOperationState) -> RuntimeOperationState:
    if state.leaf != LEAF_TOOL_DELIVERY_PENDING:
        raise RuntimeOperationTransitionError("tool delivery settlement requires delivery_pending")
    if state.driver == DRIVER_REPAIR:
        return _transition(state, LEAF_REPAIR_RUNNING)
    return _transition(state, LEAF_WRITER_RUNNING)


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
        LEAF_COMPLETION_PROOF_RECORDED,
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
    return _transition(state, LEAF_REPAIR_CONTEXT_ADMITTED, repair_context_ref=digest)


def mark_repair_running(
    state: RuntimeOperationState,
    *,
    provider_id: str,
) -> RuntimeOperationState:
    if state.repair_rounds >= state.max_repair_rounds:
        raise RuntimeOperationTransitionError("repair round budget exhausted")
    return _transition(
        state,
        LEAF_REPAIR_RUNNING,
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
        LEAF_REPAIR_SETTLED,
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
    if state.leaf != LEAF_COMPLETION_PROOF_RECORDED:
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
    if state.leaf == LEAF_TERMINAL:
        if state.terminal is not None and state.terminal.identity() == terminal.identity():
            return state
        raise RuntimeOperationTransitionError("terminal state is immutable")
    return _transition(
        state,
        LEAF_TERMINAL,
        terminal=terminal,
        blocked_reason=terminal.blocked_reason,
    )


def operation_state_entry(state: RuntimeOperationState) -> dict[str, object]:
    return {
        "lane": state.lane,
        "operation_id": state.operation_id,
        "kind": "operation_state",
        "payload": state.to_payload(),
    }


def operation_started_entry(state: RuntimeOperationState) -> dict[str, object]:
    return {
        "lane": state.lane,
        "operation_id": state.operation_id,
        "kind": "operation_started",
        "payload": {"operation_kind": state.task_kind},
    }


def start_entries(
    projection: RuntimeProjection,
    state: RuntimeOperationState,
) -> tuple[dict[str, object], ...]:
    existing = projection.operations.get(state.operation_id)
    if existing is None:
        return (operation_started_entry(state), operation_state_entry(state))
    if existing.lane != state.lane or existing.status != "open":
        raise RuntimeOperationTransitionError("task operation is not open")
    return (operation_state_entry(state),)


def operation_is_open(
    projection: RuntimeProjection,
    state: RuntimeOperationState,
) -> bool:
    existing = projection.operations.get(state.operation_id)
    if existing is None:
        return False
    if existing.lane != state.lane or existing.status != "open":
        raise RuntimeOperationTransitionError("task operation is not open")
    return True


def outcome_for_terminal(state: RuntimeOperationState) -> OperationOutcome:
    reason = state.terminal.stop_reason if state.terminal is not None else state.stop_reason
    return operation_outcome_from_stop_reason(
        reason,
        blocked_reason=state.blocked_reason,
        summary="done" if reason == "done" else "",
    )


def _transition(
    state: RuntimeOperationState,
    next_leaf: str,
    **updates: object,
) -> RuntimeOperationState:
    if next_leaf not in _ALLOWED_TRANSITIONS.get(state.leaf, frozenset()):
        raise RuntimeOperationTransitionError(f"illegal transition {state.leaf} -> {next_leaf}")
    if state.blocked_reason and next_leaf != LEAF_TERMINAL:
        raise RuntimeOperationTransitionError("blocked verdict may only finish")
    if next_leaf not in _PENDING_LEAVES:
        updates.setdefault("driver", "")
        updates.setdefault("pending_effect_category", "")
        updates.setdefault("pending_effect_ids", ())
        updates.setdefault("pending_delivery_batch_id", "")
        updates.setdefault("turn", 0)
        updates.setdefault("tool_index", 0)
    return replace(state, leaf=next_leaf, updated_at=_now(), **updates)  # type: ignore[arg-type]


def _require_pending_effect(
    state: RuntimeOperationState,
    *,
    leaf: str,
    effect_id: str,
) -> None:
    clean = _text(effect_id, "effect_id", limit=MAX_REF_CHARS)
    if state.leaf != leaf:
        raise RuntimeOperationTransitionError(f"expected {leaf}")
    if clean not in state.pending_effect_ids:
        raise RuntimeOperationTransitionError("effect is not pending on operation state")


def _validate_pending_leaf(
    *,
    leaf: str,
    driver: str,
    pending_category: str,
    pending_effect_ids: tuple[str, ...],
    pending_delivery_batch_id: str,
    turn: int,
    tool_index: int,
) -> None:
    if leaf not in _PENDING_LEAVES:
        if driver or pending_category or pending_effect_ids or pending_delivery_batch_id or turn or tool_index:
            raise RuntimeOperationTransitionError("non-pending leaf carries pending effect facts")
        return
    if driver not in DRIVERS:
        raise RuntimeOperationTransitionError("pending leaf requires driver")
    if leaf == LEAF_PROVIDER_EFFECT_PENDING:
        if pending_category != "provider_send" or len(pending_effect_ids) != 1:
            raise RuntimeOperationTransitionError("provider pending requires one provider effect")
        if tool_index:
            raise RuntimeOperationTransitionError("provider pending cannot carry tool index")
        return
    if leaf == LEAF_TOOL_EFFECT_PENDING:
        if pending_category != "tool_call" or not pending_effect_ids:
            raise RuntimeOperationTransitionError("tool pending requires tool effect ids")
        if not pending_delivery_batch_id:
            raise RuntimeOperationTransitionError("tool pending requires delivery batch id")
        return
    if leaf == LEAF_TOOL_DELIVERY_PENDING:
        if pending_category or pending_effect_ids or not pending_delivery_batch_id or tool_index:
            raise RuntimeOperationTransitionError("delivery pending carries invalid effect facts")


def _pending_effect_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise RuntimeOperationTransitionError("pending_effect_ids must be a list")
    if len(value) > MAX_PENDING_EFFECT_IDS:
        raise RuntimeOperationTransitionError("too many pending effect ids")
    out: list[str] = []
    for item in value:
        effect_id = _text(item, "pending_effect_id", limit=MAX_REF_CHARS)
        if effect_id in out:
            raise RuntimeOperationTransitionError("duplicate pending effect id")
        out.append(effect_id)
    return tuple(out)


def _is_repair_leaf(leaf: str, driver: str) -> bool:
    return leaf in _REPAIR_STRUCTURAL_LEAVES or (
        leaf in _PENDING_LEAVES and driver == DRIVER_REPAIR
    )


def _is_writer_pending_leaf(leaf: str, driver: str) -> bool:
    return leaf in _PENDING_LEAVES and driver == DRIVER_WRITER


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
    "DRIVER_REPAIR",
    "DRIVER_WRITER",
    "DRIVERS",
    "INTERRUPTED_COMPLETION_CHECK",
    "INTERRUPTED_FINISHING",
    "INTERRUPTED_REPAIR",
    "INTERRUPTED_WRITING",
    "KIND",
    "LEAF_ACCEPTED",
    "LEAF_COMPLETION_PROOF_RECORDED",
    "LEAF_PROVIDER_EFFECT_PENDING",
    "LEAF_REPAIR_CONTEXT_ADMITTED",
    "LEAF_REPAIR_RUNNING",
    "LEAF_REPAIR_SETTLED",
    "LEAF_TERMINAL",
    "LEAF_TOOL_DELIVERY_PENDING",
    "LEAF_TOOL_EFFECT_PENDING",
    "LEAF_WRITER_RUNNING",
    "LEAF_WRITER_SETTLED",
    "LEAVES",
    "SCHEMA_VERSION",
    "RuntimeOperationState",
    "RuntimeOperationStore",
    "RuntimeOperationTerminal",
    "RuntimeOperationTransitionError",
    "lane_for_run",
    "mark_completion_blocked",
    "mark_completion_proof_recorded",
    "mark_provider_effect_pending",
    "mark_provider_effect_settled",
    "mark_repair_context_admitted",
    "mark_repair_running",
    "mark_repair_settled",
    "mark_terminal",
    "mark_tool_delivery_pending",
    "mark_tool_delivery_settled",
    "mark_tool_effect_pending",
    "mark_tool_effect_settled",
    "mark_writer_running",
    "mark_writer_settled",
    "new_operation_state",
    "operation_id_for_run",
    "operation_is_open",
    "operation_progress_text",
    "operation_started_entry",
    "operation_state_entry",
    "operation_state_from_entries",
    "outcome_for_terminal",
    "start_entries",
]
