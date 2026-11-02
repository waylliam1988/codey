"""Durable operation state for Codey's runtime core.

OperationState is a first-class log record. It answers "where may recovery
start, and which next action is legal?" External effects remain in the effect
ledger; UI events and run traces are observation only.
"""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from codey.runtime.core.operation_payload import (
    _BLOCKABLE_PROOF_STATUSES,
    _COMPLETION_PROOF_REF_RE,
    _PENDING_LEAVES,
    _RECORDED_PROOF_STATUSES,
    _REPAIR_SOURCE_PROOF_STATUSES,
    _RESPONSE_LEAVES,
    _SHA256_REF_RE,
    _WORK_LEAVES,
    DRIVER_REPAIR,
    DRIVER_WRITER,
    DRIVERS,
    INTERRUPTED_COMPLETION_CHECK,
    INTERRUPTED_FINISHING,
    INTERRUPTED_REPAIR,
    INTERRUPTED_WRITING,
    LEAF_ACCEPTED,
    LEAF_CLOSING,
    LEAF_COMPLETION_PENDING,
    LEAF_COMPLETION_PROOF_RECORDED,
    LEAF_CONTROL_READY,
    LEAF_KERNEL_SETTLED,
    LEAF_PLAN_READY,
    LEAF_PROVIDER_EFFECT_PENDING,
    LEAF_PROVIDER_REQUEST_READY,
    LEAF_PROVIDER_RESPONSE_READY,
    LEAF_REPAIR_CONTEXT_ADMITTED,
    LEAF_REPAIR_RUNNING,
    LEAF_REPAIR_SETTLED,
    LEAF_TERMINAL,
    LEAF_TOOL_DELIVERY_PENDING,
    LEAF_TOOL_EFFECT_PENDING,
    LEAF_WRITER_RUNNING,
    LEAF_WRITER_SETTLED,
    MAX_ID_CHARS,
    MAX_REF_CHARS,
    RuntimeExecutionCounters,
    RuntimeOperationState,
    RuntimeOperationTerminal,
    RuntimeOperationTransitionError,
    _count,
    _is_repair_leaf,
    _parse_final_delivery,
    _parse_model_selection,
    _parse_task_policy,
    _text,
)
from codey.runtime.core.outcome import OperationOutcome, operation_outcome_from_stop_reason
from codey.runtime.log.entries import RuntimeLogEntry
from codey.runtime.log.session_projection import RuntimeProjection
from codey.storage.local_store import project_key, session_key

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    LEAF_ACCEPTED: frozenset({
        LEAF_TERMINAL,
        LEAF_WRITER_RUNNING,
    }),
    LEAF_WRITER_RUNNING: frozenset({
        LEAF_CLOSING,
        LEAF_PROVIDER_EFFECT_PENDING,
        LEAF_PROVIDER_REQUEST_READY,
        LEAF_PROVIDER_RESPONSE_READY,
        LEAF_TERMINAL,
        LEAF_TOOL_DELIVERY_PENDING,
        LEAF_TOOL_EFFECT_PENDING,
        LEAF_WRITER_SETTLED,
    }),
    LEAF_PROVIDER_EFFECT_PENDING: frozenset({
        LEAF_CONTROL_READY,
        LEAF_PROVIDER_RESPONSE_READY,
        LEAF_REPAIR_RUNNING,
        LEAF_TERMINAL,
        LEAF_TOOL_DELIVERY_PENDING,
        LEAF_TOOL_EFFECT_PENDING,
        LEAF_WRITER_RUNNING,
    }),
    LEAF_PROVIDER_RESPONSE_READY: frozenset({
        LEAF_CLOSING,
        LEAF_COMPLETION_PENDING,
        LEAF_CONTROL_READY,
        LEAF_PLAN_READY,
        LEAF_PROVIDER_REQUEST_READY,
        LEAF_REPAIR_RUNNING,
        LEAF_TERMINAL,
        LEAF_TOOL_DELIVERY_PENDING,
        LEAF_TOOL_EFFECT_PENDING,
        LEAF_WRITER_RUNNING,
    }),
    LEAF_TOOL_EFFECT_PENDING: frozenset({
        LEAF_TERMINAL,
        LEAF_TOOL_DELIVERY_PENDING,
        LEAF_TOOL_EFFECT_PENDING,
    }),
    LEAF_TOOL_DELIVERY_PENDING: frozenset({
        LEAF_CLOSING,
        LEAF_COMPLETION_PENDING,
        LEAF_PROVIDER_EFFECT_PENDING,
        LEAF_PROVIDER_REQUEST_READY,
        LEAF_REPAIR_RUNNING,
        LEAF_TERMINAL,
        LEAF_WRITER_RUNNING,
    }),
    LEAF_WRITER_SETTLED: frozenset({
        LEAF_COMPLETION_PROOF_RECORDED,
        LEAF_TERMINAL,
        LEAF_WRITER_RUNNING,
    }),
    LEAF_COMPLETION_PROOF_RECORDED: frozenset({
        LEAF_REPAIR_CONTEXT_ADMITTED,
        LEAF_TERMINAL,
    }),
    LEAF_REPAIR_CONTEXT_ADMITTED: frozenset({
        LEAF_REPAIR_RUNNING,
        LEAF_TERMINAL,
    }),
    LEAF_REPAIR_RUNNING: frozenset({
        LEAF_CLOSING,
        LEAF_PROVIDER_EFFECT_PENDING,
        LEAF_PROVIDER_REQUEST_READY,
        LEAF_PROVIDER_RESPONSE_READY,
        LEAF_REPAIR_SETTLED,
        LEAF_TERMINAL,
        LEAF_TOOL_DELIVERY_PENDING,
        LEAF_TOOL_EFFECT_PENDING,
    }),
    LEAF_REPAIR_SETTLED: frozenset({
        LEAF_COMPLETION_PROOF_RECORDED,
        LEAF_REPAIR_RUNNING,
        LEAF_TERMINAL,
    }),
    LEAF_TERMINAL: frozenset({
        LEAF_TERMINAL,
    }),
    LEAF_PROVIDER_REQUEST_READY: frozenset({
        LEAF_CLOSING,
        LEAF_PROVIDER_EFFECT_PENDING,
    }),
    LEAF_PLAN_READY: frozenset({
        LEAF_CLOSING,
        LEAF_CONTROL_READY,
        LEAF_TOOL_EFFECT_PENDING,
    }),
    LEAF_COMPLETION_PENDING: frozenset({
        LEAF_CLOSING,
        LEAF_CONTROL_READY,
    }),
    LEAF_CONTROL_READY: frozenset({
        LEAF_CLOSING,
        LEAF_COMPLETION_PENDING,
        LEAF_KERNEL_SETTLED,
        LEAF_PROVIDER_REQUEST_READY,
    }),
    LEAF_CLOSING: frozenset({
        LEAF_CLOSING,
        LEAF_KERNEL_SETTLED,
        LEAF_PROVIDER_REQUEST_READY,
    }),
    LEAF_KERNEL_SETTLED: frozenset({
        LEAF_CLOSING,
        LEAF_REPAIR_RUNNING,
        LEAF_REPAIR_SETTLED,
        LEAF_TERMINAL,
        LEAF_WRITER_RUNNING,
        LEAF_WRITER_SETTLED,
    }),
}


class RuntimeOperationStore:
    """Read projection for one run's durable operation state."""

    def __init__(self, *, durable: Any) -> None:
        if durable is None:
            raise ValueError("an operation store requires durable storage")
        self.durable = durable

    def load(self, session_id: str, run_id: str) -> RuntimeOperationState | None:
        try:
            task = self.durable.task(run_id)
        except KeyError:
            return None
        checkpoint = cast(_CheckpointAdapter, importlib.import_module("codey.durable.checkpoint"))
        return checkpoint.operation_from_checkpoint(task, session_id)


class _CheckpointAdapter(Protocol):
    def operation_from_checkpoint(
        self, task: object, session_id: str,
    ) -> RuntimeOperationState | None: ...


def operation_id_for_run(run_id: str) -> str:
    return "task:" + hashlib.sha256((run_id or "").encode("utf-8")).hexdigest()[:24]


def lane_for_run(run_id: str) -> str:
    return "run:" + session_key(run_id or "")[:24]


def new_operation_state(
    *,
    session_id: str,
    run_id: str,
    project: object = "",
    provider_id: str,
    turn_budget: int,
    max_repair_rounds: int,
    task_kind: str = "task",
    task_policy: dict[str, object] | None = None,
    model_selection: dict[str, object] | None = None,
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
        task_policy=_parse_task_policy(task_policy),
        model_selection=_parse_model_selection(model_selection),
    )


def operation_state_from_entries(
    entries: Iterable[RuntimeLogEntry],
    *,
    session_id: str,
    run_id: str,
) -> RuntimeOperationState | None:
    operation_id = operation_id_for_run(run_id or "")
    lane = lane_for_run(run_id or "")
    latest: RuntimeOperationState | None = None
    for entry in entries:
        if entry.kind != "operation_state" or entry.operation_id != operation_id:
            continue
        if entry.lane != lane:
            raise RuntimeOperationTransitionError("operation state entry boundary mismatch")
        state = RuntimeOperationState.from_payload(entry.payload)
        if state is None:
            raise RuntimeOperationTransitionError("invalid operation state payload")
        if (
            state.session_id != session_id
            or state.run_id != run_id
            or state.operation_id != operation_id
            or state.lane != lane
        ):
            raise RuntimeOperationTransitionError("operation state payload boundary mismatch")
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
    if state.leaf == LEAF_COMPLETION_PROOF_RECORDED and completion_proof_satisfied(state):
        return INTERRUPTED_FINISHING
    return INTERRUPTED_COMPLETION_CHECK


def mark_writer_running(
    state: RuntimeOperationState,
    *,
    provider_id: str,
    writer_attempt: int | None = None,
) -> RuntimeOperationState:
    provider = _text(provider_id, "provider_id")
    attempt = state.writer_attempt if writer_attempt is None else _count(writer_attempt, "writer_attempt", minimum=1)
    if (state.task_kind == "research" and state.writer_attempt > 0
            and writer_attempt is not None and attempt != state.writer_attempt):
        # Research follow-ups have fixed attempt coordinates. Restarting the
        # same invocation keeps its receipts; only its explicit successor may
        # start fresh after the previous kernel has settled. Charge the settled
        # invocation here, atomically with that successor, exactly once.
        if (attempt <= state.writer_attempt or state.leaf != LEAF_KERNEL_SETTLED
                or state.driver != DRIVER_WRITER or state.provider_id != provider):
            raise RuntimeOperationTransitionError("research invocation requires its settled predecessor")
        return _transition(state, LEAF_WRITER_RUNNING, writer_attempt=attempt,
                           turns_used=_count(state.turns_used + state.logical_turn, "turns_used", maximum=state.turn_budget),
                           counters=RuntimeExecutionCounters(), logical_turn=0, stop_reason="",
                           work_ref="", request_ref="", response_ref="")
    if state.leaf in _PENDING_LEAVES - {LEAF_TOOL_DELIVERY_PENDING}:
        if state.driver != DRIVER_WRITER or state.provider_id != provider or state.writer_attempt != attempt:
            raise RuntimeOperationTransitionError("pending writer identity cannot be rebound")
        return state
    if state.leaf == LEAF_WRITER_SETTLED:
        if state.stop_reason in {"question", "approval"} and attempt == state.writer_attempt:
            return _transition(state, LEAF_WRITER_RUNNING, provider_id=provider,
                               writer_attempt=attempt + 1, stop_reason="")
        # Review may request another writer attempt before the final proof.
        # The caller must advance the attempt; failed/finalized work cannot
        # be reopened and settled verdict fields never survive into writing.
        if attempt <= state.writer_attempt or state.stop_reason != "done" or state.completion_proof_ref:
            raise RuntimeOperationTransitionError("writer restart requires a new attempt after done before final proof")
        return _transition(state, LEAF_WRITER_RUNNING, provider_id=provider,
                           writer_attempt=attempt, turns_used=state.turns_used, stop_reason="",
                           counters=RuntimeExecutionCounters(), logical_turn=0)
    if state.leaf in {LEAF_WRITER_RUNNING, LEAF_TOOL_DELIVERY_PENDING}:
        # Rebinding a writer changes transport metadata, never acknowledges
        # delivery or restarts the task state machine.
        if state.provider_id == provider and state.writer_attempt == attempt:
            return state
        return replace(state, provider_id=provider, writer_attempt=attempt, updated_at=_now())
    return _transition(
        state,
        LEAF_WRITER_RUNNING,
        provider_id=provider,
        writer_attempt=attempt,
    )


def mark_candidate_validation_running(
    state: RuntimeOperationState,
    *,
    provider_id: str,
    writer_attempt: int,
) -> RuntimeOperationState:
    """Admit one candidate check across writer and bounded repair phases.

    Authorization and check selection belong to the operation/kernel. This
    transition only protects the durable phase, identity and remaining budget.
    """
    provider = _text(provider_id, "provider_id")
    attempt = _count(writer_attempt, 'writer_attempt', minimum=state.writer_attempt + 1)
    repair = state.leaf == LEAF_REPAIR_SETTLED
    if (state.leaf not in {LEAF_WRITER_SETTLED, LEAF_REPAIR_SETTLED} or state.stop_reason != "no_progress"
            or state.candidate_validation_attempted or state.blocked_reason
            or (not repair and state.completion_proof_ref)
            or (repair and (state.completion_proof_status != 'failed' or not state.repair_context_ref
                            or state.repair_rounds != 1))
            or state.turn_budget - state.turns_used < 2 or state.provider_id != provider
            or state.task_kind != "project"):
        raise RuntimeOperationTransitionError("stopped candidate validation admission is unavailable")
    return _transition(state, LEAF_REPAIR_RUNNING if repair else LEAF_WRITER_RUNNING,
                       writer_attempt=attempt, candidate_validation_attempted=True, stop_reason="",
                       counters=RuntimeExecutionCounters(), logical_turn=0)


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


def mark_provider_request_ready(
    state: RuntimeOperationState, *, request_ref: str, driver: str, turn: int,
) -> RuntimeOperationState:
    """Freeze outbound work before closing the preceding received generation."""
    if driver not in DRIVERS:
        raise RuntimeOperationTransitionError("prepared request requires a writer or repair identity")
    return _transition(state, LEAF_PROVIDER_REQUEST_READY,
        request_ref=_text(request_ref, "request_ref"), driver=driver,
        turn=_count(turn, "turn"), tool_index=0,
        response_ref=state.response_ref if state.leaf == LEAF_PROVIDER_RESPONSE_READY else "")


def mark_provider_effect_pending(
    state: RuntimeOperationState,
    *,
    driver: str,
    provider_id: str,
    turn: int,
) -> RuntimeOperationState:
    if driver not in DRIVERS:
        raise RuntimeOperationTransitionError("provider effect driver must be writer or repair")
    return _transition(
        state,
        LEAF_PROVIDER_EFFECT_PENDING,
        provider_id=_text(provider_id, "provider_id"),
        driver=driver,
        turn=_count(turn, "turn"),
        tool_index=0,
    )


def mark_provider_effect_settled(
    state: RuntimeOperationState,
    *, delivery_turn: int | None = None,
) -> RuntimeOperationState:
    # A generation may defer the provider settlement until a post-tools or
    # final boundary. Tool intents can therefore be pending while the
    # provider effect is still unsettled. The effect projection proves which
    # provider record is being settled; preserve the tool delivery state here.
    if state.leaf == LEAF_PROVIDER_REQUEST_READY:
        return replace(state, response_ref="", updated_at=_now())
    if state.leaf == LEAF_PROVIDER_EFFECT_PENDING and state.work_ref:
        # A failed control acknowledgement retains its saved decision. Its
        # request artifact still references the original response and call ids.
        return _transition(state, LEAF_CONTROL_READY)
    if state.leaf == LEAF_PROVIDER_EFFECT_PENDING and delivery_turn is not None:
        # A proven unsent result delivery still owns the original tool batch.
        # Its agent turn is distinct from the failed provider send counter.
        return replace(mark_tool_delivery_pending(state, driver=state.driver, turn=delivery_turn),
                       logical_turn=delivery_turn)
    if state.leaf in _RESPONSE_LEAVES | {LEAF_KERNEL_SETTLED}:
        return state
    if state.leaf in {LEAF_WRITER_RUNNING, LEAF_REPAIR_RUNNING}:
        return state
    if state.leaf not in {LEAF_PROVIDER_EFFECT_PENDING, LEAF_PROVIDER_RESPONSE_READY}:
        raise RuntimeOperationTransitionError(f"expected {LEAF_PROVIDER_EFFECT_PENDING}")
    if state.driver == DRIVER_REPAIR:
        return _transition(state, LEAF_REPAIR_RUNNING)
    return _transition(state, LEAF_WRITER_RUNNING)


def mark_provider_response_ready(state: RuntimeOperationState, *, response_ref: str) -> RuntimeOperationState:
    """The response body is reliable; the generation boundary is still open."""
    return _transition(state, LEAF_PROVIDER_RESPONSE_READY,
                       response_ref=_text(response_ref, "response_ref"))


def advance_execution(
    state: RuntimeOperationState, leaf: str, *, logical_turn: int | None = None,
    work_ref: str | None = None, counters: RuntimeExecutionCounters | None = None,
) -> RuntimeOperationState:
    """Advance the existing execution position with typed, reference-only data."""
    updates: dict[str, object] = {}
    if logical_turn is not None:
        updates["logical_turn"] = _count(logical_turn, "logical_turn")
    if work_ref is not None:
        updates["work_ref"] = _text(work_ref, "work_ref", allow_empty=True)
    if counters is not None:
        updates["counters"] = RuntimeExecutionCounters.from_payload(counters.to_payload())
    if leaf in _PENDING_LEAVES:
        updates.update(driver=state.driver or (DRIVER_REPAIR if _is_repair_leaf(state.leaf, state.driver) else DRIVER_WRITER),
                       turn=state.turn, tool_index=0)
    return _transition(state, leaf, **updates)


def mark_tool_effect_pending(
    state: RuntimeOperationState,
    *,
    driver: str,
    turn: int,
    tool_index: int = 0,
) -> RuntimeOperationState:
    if driver not in DRIVERS:
        raise RuntimeOperationTransitionError("tool effect driver must be writer or repair")
    return _transition(
        state,
        LEAF_TOOL_EFFECT_PENDING,
        driver=driver,
        turn=_count(turn, "turn"),
        tool_index=_count(tool_index, "tool_index"),
    )


def mark_tool_effect_settled(
    state: RuntimeOperationState,
    *,
    has_remaining: bool,
) -> RuntimeOperationState:
    if state.leaf != LEAF_TOOL_EFFECT_PENDING:
        raise RuntimeOperationTransitionError(f"expected {LEAF_TOOL_EFFECT_PENDING}")
    if has_remaining:
        return _transition(
            state,
            LEAF_TOOL_EFFECT_PENDING,
            driver=state.driver,
            turn=state.turn,
            tool_index=state.tool_index,
        )
    return mark_tool_delivery_pending(
        state,
        driver=state.driver,
        turn=state.turn,
    )


def mark_tool_delivery_pending(
    state: RuntimeOperationState,
    *,
    driver: str,
    turn: int,
) -> RuntimeOperationState:
    if driver not in DRIVERS:
        raise RuntimeOperationTransitionError("tool delivery driver must be writer or repair")
    return _transition(
        state,
        LEAF_TOOL_DELIVERY_PENDING,
        driver=driver,
        turn=_count(turn, "turn"),
        tool_index=0,
    )


def mark_tool_delivery_settled(state: RuntimeOperationState) -> RuntimeOperationState:
    if state.leaf != LEAF_TOOL_DELIVERY_PENDING:
        raise RuntimeOperationTransitionError("tool delivery settlement requires delivery_pending")
    if state.driver == DRIVER_REPAIR:
        return _transition(state, LEAF_REPAIR_RUNNING)
    return _transition(state, LEAF_WRITER_RUNNING)


def completion_proof_satisfied(state: RuntimeOperationState) -> bool | None:
    """Derive proof satisfaction from the recorded status.

    Satisfaction is a judgment over the durable ref/status facts, never a
    stored fact itself: a recorded `complete` status satisfies, any other
    recorded status does not, and no ref means unobserved.
    """
    if not state.completion_proof_ref:
        return None
    return state.completion_proof_status == "complete"


def mark_completion_proof_recorded(
    state: RuntimeOperationState,
    *,
    proof_ref: str,
    proof_status: str,
) -> RuntimeOperationState:
    ref = _text(proof_ref, "proof_ref", limit=MAX_REF_CHARS)
    status = _text(proof_status, "proof_status")
    if not _COMPLETION_PROOF_REF_RE.match(ref):
        raise RuntimeOperationTransitionError("proof_ref must be completion proof ref")
    if status not in _RECORDED_PROOF_STATUSES:
        raise RuntimeOperationTransitionError("unknown proof status")
    return _transition(
        state,
        LEAF_COMPLETION_PROOF_RECORDED,
        completion_proof_ref=ref,
        completion_proof_status=status,
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
        counters=RuntimeExecutionCounters(), logical_turn=0,
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
    verdict = _text(reason, "reason")
    if state.blocked_reason == verdict:
        return state
    if state.blocked_reason:
        raise RuntimeOperationTransitionError("blocked verdict is immutable")
    return replace(state, blocked_reason=verdict, updated_at=_now())


def mark_terminal(
    state: RuntimeOperationState,
    *,
    stop_reason: str,
    summary_chars: int,
    turns: int,
    max_turns: int,
    provider: str,
    blocked_reason: str | None = None,
    final_delivery: str | None = None,
    proof_ref: str = "",
    proof_status: str = "",
) -> RuntimeOperationState:
    delivery = state.final_delivery if final_delivery is None else _parse_final_delivery(final_delivery)
    if proof_ref:
        if not _COMPLETION_PROOF_REF_RE.fullmatch(proof_ref) or proof_status not in _RECORDED_PROOF_STATUSES:
            raise RuntimeOperationTransitionError("invalid terminal completion proof")
        if state.completion_proof_ref and state.completion_proof_ref != proof_ref:
            raise RuntimeOperationTransitionError("terminal cannot replace completion proof")
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
        if state.terminal is not None and state.terminal.identity() == terminal.identity() and state.final_delivery == delivery:
            return state
        raise RuntimeOperationTransitionError("terminal state is immutable")
    return _transition(
        state,
        LEAF_TERMINAL,
        terminal=terminal,
        final_delivery=delivery,
        completion_proof_ref=proof_ref or state.completion_proof_ref,
        completion_proof_status=proof_status or state.completion_proof_status,
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
    if next_leaf not in _RESPONSE_LEAVES | {LEAF_PROVIDER_REQUEST_READY}:
        updates.setdefault("response_ref", "")
    if next_leaf not in _WORK_LEAVES:
        updates.setdefault("work_ref", "")
    if next_leaf != LEAF_PROVIDER_REQUEST_READY:
        updates.setdefault("request_ref", "")
    if next_leaf not in _PENDING_LEAVES:
        updates.setdefault("driver", "")
        updates.setdefault("turn", 0)
        updates.setdefault("tool_index", 0)
    return replace(state, leaf=next_leaf, updated_at=_now(), **updates)  # type: ignore[arg-type]


def _repair_candidate_proof(state: RuntimeOperationState) -> bool:
    return (
        bool(state.completion_proof_ref)
        and state.completion_proof_status in _REPAIR_SOURCE_PROOF_STATUSES
        and completion_proof_satisfied(state) is False
    )


def _blocked_verdict_supported(state: RuntimeOperationState) -> bool:
    return (
        bool(state.completion_proof_ref)
        and state.completion_proof_status in _BLOCKABLE_PROOF_STATUSES
        and completion_proof_satisfied(state) is False
    )


def _project_ref(project: object) -> str:
    text = str(project or "").strip()
    return f"project:{project_key(text)}" if text else ""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_id(value: object) -> str:
    return _text(value, "id", limit=MAX_ID_CHARS)


__all__ = [
    "completion_proof_satisfied",
    "RuntimeOperationStore",
    "lane_for_run",
    "mark_completion_blocked",
    "mark_completion_proof_recorded",
    "mark_provider_effect_pending",
    "mark_provider_request_ready",
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
    "mark_candidate_validation_running",
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
