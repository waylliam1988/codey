"""Persisted operation schema and validation; it owns no execution or writes."""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field, replace

from codey.runtime.core.progress import ProgressSnapshot


class RuntimeOperationTransitionError(Exception):
    """An operation state transition violated the closed leaf table."""


@dataclass(frozen=True)
class RuntimeExecutionCounters:
    """Bounded execution history; it carries no independent execution phase."""

    invalid_turns: int = 0
    length_continuation_used: bool = False
    closing_round: int = 0
    progress: ProgressSnapshot = field(default_factory=ProgressSnapshot)

    def to_payload(self) -> dict[str, object]:
        return {"invalid_turns": self.invalid_turns,
                "length_continuation_used": self.length_continuation_used,
                "closing_round": self.closing_round, "progress": self.progress.to_payload()}

    @classmethod
    def from_payload(cls, value: object) -> RuntimeExecutionCounters:
        if (not isinstance(value, dict)
                or set(value) != {"invalid_turns", "length_continuation_used", "closing_round", "progress"}
                or type(value["invalid_turns"]) is not int or value["invalid_turns"] < 0
                or type(value["length_continuation_used"]) is not bool
                or type(value["closing_round"]) is not int or not 0 <= value["closing_round"] <= 4):
            raise RuntimeOperationTransitionError("invalid operation execution counters")
        try:
            progress = ProgressSnapshot.from_payload(value["progress"])
        except ValueError as exc:
            raise RuntimeOperationTransitionError(str(exc)) from exc
        return cls(value["invalid_turns"], value["length_continuation_used"], value["closing_round"], progress)


def _parse_final_delivery(value: object) -> str:
    if not isinstance(value, str) or value not in {"not_required", "success", "failed", "unknown"}:
        raise RuntimeOperationTransitionError("invalid final model result delivery")
    return value


def _parse_model_selection(value: object) -> dict[str, object]:
    if value is None or value == {}:
        return {}
    from codey.runtime.core.api_selection import ApiRunSelection

    try:
        return ApiRunSelection.from_payload(value).to_payload()
    except ValueError as exc:
        raise RuntimeOperationTransitionError("invalid admitted API selection") from exc


def _parse_task_policy(value: object) -> dict[str, object]:
    """Preserve opaque authorization data; TaskPolicy owns its schema.

    Runtime storage must neither drop newly added fields nor turn invalid
    values into valid ones. Semantic validation belongs to the policy owner.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RuntimeOperationTransitionError("task_policy must be an object")
    try:
        json.dumps(value, allow_nan=False)
        return copy.deepcopy(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeOperationTransitionError("task_policy must be JSON data") from exc


SCHEMA_VERSION = 6

KIND = "runtime_operation_state"

LEAF_ACCEPTED = "accepted"

LEAF_WRITER_RUNNING = "writer_running"

LEAF_PROVIDER_REQUEST_READY = "provider_request_ready"

LEAF_PROVIDER_EFFECT_PENDING = "provider_effect_pending"

LEAF_PROVIDER_RESPONSE_READY = "provider_response_ready"

LEAF_PLAN_READY = "plan_ready"

LEAF_COMPLETION_PENDING = "completion_pending"

LEAF_CONTROL_READY = "control_ready"

LEAF_CLOSING = "closing"

LEAF_KERNEL_SETTLED = "kernel_settled"

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
    LEAF_PROVIDER_REQUEST_READY,
    LEAF_PROVIDER_EFFECT_PENDING,
    LEAF_PROVIDER_RESPONSE_READY,
    LEAF_PLAN_READY, LEAF_COMPLETION_PENDING, LEAF_CONTROL_READY, LEAF_CLOSING, LEAF_KERNEL_SETTLED,
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
    LEAF_PROVIDER_REQUEST_READY,
    LEAF_PROVIDER_EFFECT_PENDING,
    LEAF_PROVIDER_RESPONSE_READY,
    LEAF_PLAN_READY, LEAF_COMPLETION_PENDING, LEAF_CONTROL_READY, LEAF_CLOSING, LEAF_KERNEL_SETTLED,
    LEAF_TOOL_EFFECT_PENDING,
    LEAF_TOOL_DELIVERY_PENDING,
})

_RESPONSE_LEAVES = frozenset({LEAF_PROVIDER_RESPONSE_READY, LEAF_PLAN_READY, LEAF_COMPLETION_PENDING,
    LEAF_CONTROL_READY, LEAF_CLOSING, LEAF_TOOL_EFFECT_PENDING, LEAF_TOOL_DELIVERY_PENDING})

_WORK_LEAVES = frozenset({LEAF_CONTROL_READY, LEAF_CLOSING, LEAF_KERNEL_SETTLED,
    LEAF_PROVIDER_REQUEST_READY, LEAF_PROVIDER_EFFECT_PENDING, LEAF_PROVIDER_RESPONSE_READY})

MAX_TEXT_CHARS = 80

MAX_REF_CHARS = 160

MAX_PROJECT_REF_CHARS = 240

MAX_ID_CHARS = 200

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

_KNOWN_PAYLOAD_KEYS = frozenset({
    "schema_version", "kind", "session_id", "run_id", "operation_id", "lane",
    "project_ref", "provider_id", "turn_budget", "max_repair_rounds", "leaf",
    "started_at", "updated_at", "task_kind", "task_policy",
    "model_selection", "reviewer_selection", "final_delivery",
    "writer_attempt", "candidate_validation_attempted", "turns_used", "stop_reason",
    "completion_proof_ref", "completion_proof_status", "repair_rounds", "continuation_ref",
    "repair_context_ref", "blocked_reason", "driver", "turn", "tool_index", "terminal", "response_ref", "request_ref", "counters", "work_ref", "logical_turn",
})

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
    def from_payload(cls, payload: object) -> RuntimeOperationTerminal | None:
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

    def identity(self) -> RuntimeOperationTerminal:
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
    task_policy: dict[str, object] = field(default_factory=dict)
    model_selection: dict[str, object] = field(default_factory=dict)
    reviewer_selection: dict[str, object] = field(default_factory=dict)
    final_delivery: str = "not_required"
    writer_attempt: int = 1
    candidate_validation_attempted: bool = False
    turns_used: int = 0
    stop_reason: str = ""
    completion_proof_ref: str = ""
    completion_proof_status: str = ""
    repair_rounds: int = 0
    repair_context_ref: str = ""
    blocked_reason: str = ""
    driver: str = ""
    turn: int = 0
    tool_index: int = 0
    response_ref: str = ""
    request_ref: str = ""
    work_ref: str = ""
    continuation_ref: str = ""
    logical_turn: int = 0
    counters: RuntimeExecutionCounters = field(default_factory=RuntimeExecutionCounters)
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
            "candidate_validation_attempted": self.candidate_validation_attempted,
            "turns_used": self.turns_used,
            "stop_reason": self.stop_reason,
            "completion_proof_ref": self.completion_proof_ref,
            "completion_proof_status": self.completion_proof_status,
            "repair_rounds": self.repair_rounds,
            "repair_context_ref": self.repair_context_ref,
            "blocked_reason": self.blocked_reason,
            "driver": self.driver,
            "turn": self.turn,
            "tool_index": self.tool_index,
            "counters": self.counters.to_payload(),
            "logical_turn": self.logical_turn,
        }
        if self.final_delivery != "not_required":
            payload["final_delivery"] = _parse_final_delivery(self.final_delivery)
        if self.response_ref:
            payload["response_ref"] = self.response_ref
        if self.work_ref:
            payload["work_ref"] = self.work_ref
        if self.continuation_ref:
            payload["continuation_ref"] = self.continuation_ref
        if self.request_ref:
            payload["request_ref"] = self.request_ref
        if self.model_selection:
            payload["model_selection"] = _parse_model_selection(self.model_selection)
        if self.reviewer_selection:
            payload["reviewer_selection"] = _parse_model_selection(self.reviewer_selection)
        if self.task_policy:
            payload["task_policy"] = dict(self.task_policy)
        if self.terminal is not None:
            payload["terminal"] = self.terminal.to_payload()
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> RuntimeOperationState | None:
        if not isinstance(payload, dict):
            return None
        schema_version = payload.get("schema_version")
        if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
            return None
        if payload.get("kind") != KIND:
            return None
        try:
            if set(payload) - _KNOWN_PAYLOAD_KEYS:
                raise RuntimeOperationTransitionError("runtime operation state carries unknown keys")
            session_id, run_id, operation_id, lane, leaf = _parse_operation_identity(payload)
            response_ref = _text(payload.get("response_ref", ""), "response_ref", allow_empty=True)
            if ((leaf in {LEAF_PROVIDER_RESPONSE_READY, LEAF_PLAN_READY, LEAF_COMPLETION_PENDING} and not response_ref)
                    or (response_ref and leaf not in _RESPONSE_LEAVES | {LEAF_PROVIDER_REQUEST_READY})):
                raise RuntimeOperationTransitionError("response reference does not match operation leaf")
            request_ref = _text(payload.get("request_ref", ""), "request_ref", allow_empty=True)
            if bool(request_ref) != (leaf == LEAF_PROVIDER_REQUEST_READY):
                raise RuntimeOperationTransitionError("request reference does not match operation leaf")
            work_ref = _text(payload.get("work_ref", ""), "work_ref", allow_empty=True)
            if ((leaf in {LEAF_CONTROL_READY, LEAF_CLOSING, LEAF_KERNEL_SETTLED} and not work_ref)
                    or (work_ref and leaf not in _WORK_LEAVES)):
                raise RuntimeOperationTransitionError("decision reference does not match operation leaf")
            turn_budget, max_repair_rounds, turns_used, repair_rounds, turn, tool_index = (
                _parse_operation_counts(payload)
            )
            project_ref, writer_attempt, stop_reason = _parse_operation_project(payload)
            validation_attempted = payload.get('candidate_validation_attempted')
            if type(validation_attempted) is not bool or (validation_attempted and writer_attempt < 2):
                raise RuntimeOperationTransitionError('invalid candidate validation consumption')
            proof_ref, proof_status, satisfied, repair_context_ref, driver = _parse_operation_proof(
                payload,
                leaf=leaf,
                turn=turn,
                tool_index=tool_index,
            )
            repair_leaf = _require_repair_leaf_consistent(
                leaf=leaf,
                driver=driver,
                repair_context_ref=repair_context_ref,
                proof_status=proof_status,
                repair_rounds=repair_rounds,
            )
            _require_proof_stage_consistent(
                leaf=leaf,
                driver=driver,
                repair_leaf=repair_leaf,
                proof_ref=proof_ref,
                proof_status=proof_status,
                satisfied=satisfied,
                repair_rounds=repair_rounds,
                repair_context_ref=repair_context_ref,
            )
            blocked_reason = _parse_operation_verdict(
                payload,
                leaf=leaf,
                proof_ref=proof_ref,
                proof_status=proof_status,
                satisfied=satisfied,
            )
            _require_fresh_leaf_facts(
                leaf=leaf,
                writer_attempt=writer_attempt,
                turns_used=turns_used,
                stop_reason=stop_reason,
            )
            terminal = _parse_operation_terminal(
                payload,
                leaf=leaf,
                turn_budget=turn_budget,
                blocked_reason=blocked_reason,
            )
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
                task_policy=_parse_task_policy(payload.get("task_policy")),
                model_selection=_parse_model_selection(payload.get("model_selection")),
                reviewer_selection=_parse_model_selection(payload.get("reviewer_selection")),
                final_delivery=_parse_final_delivery(payload.get("final_delivery", "not_required")),
                writer_attempt=writer_attempt,
                candidate_validation_attempted=validation_attempted,
                turns_used=turns_used,
                stop_reason=stop_reason,
                completion_proof_ref=proof_ref,
                completion_proof_status=proof_status,
                repair_rounds=repair_rounds,
                repair_context_ref=repair_context_ref,
                blocked_reason=blocked_reason,
                driver=driver,
                turn=turn,
                tool_index=tool_index,
                response_ref=response_ref,
                request_ref=request_ref,
                work_ref=work_ref,
                continuation_ref=_parse_continuation_ref(payload),
                logical_turn=_count(payload.get("logical_turn", 0), "logical_turn"),
                counters=RuntimeExecutionCounters.from_payload(payload.get("counters")),
                terminal=terminal,
            )
        except RuntimeOperationTransitionError:
            return None

def _parse_operation_identity(payload: dict[str, object]) -> tuple[str, str, str, str, str]:
    session_id = _text(payload.get("session_id"), "session_id", limit=MAX_ID_CHARS)
    run_id = _text(payload.get("run_id"), "run_id", limit=MAX_ID_CHARS)
    operation_id = _text(payload.get("operation_id"), "operation_id", limit=MAX_ID_CHARS)
    lane = _text(payload.get("lane"), "lane", limit=MAX_ID_CHARS)
    leaf = _text(payload.get("leaf"), "leaf", limit=48)
    if leaf not in LEAVES:
        raise RuntimeOperationTransitionError("unknown operation leaf")
    return session_id, run_id, operation_id, lane, leaf

def _parse_continuation_ref(payload: dict[str, object]) -> str:
    reference = _text(payload.get("continuation_ref", ""), "continuation_ref", allow_empty=True)
    if reference and (payload.get("task_kind") != "research" or not re.fullmatch(r"artifact_[0-9a-f]{24}", reference)):
        raise RuntimeOperationTransitionError("invalid research continuation reference")
    return reference

def _parse_operation_counts(payload: dict[str, object]) -> tuple[int, int, int, int, int, int]:
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
    return turn_budget, max_repair_rounds, turns_used, repair_rounds, turn, tool_index

def _parse_operation_project(payload: dict[str, object]) -> tuple[str, int, str]:
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
    return project_ref, writer_attempt, stop_reason

def _parse_operation_proof(
    payload: dict[str, object],
    *,
    leaf: str,
    turn: int,
    tool_index: int,
) -> tuple[str, str, object, str, str]:
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
    satisfied = (proof_status == "complete") if proof_ref else None
    repair_context_ref = _text(
        payload.get("repair_context_ref"),
        "repair_context_ref",
        limit=MAX_REF_CHARS,
        allow_empty=True,
    )
    if repair_context_ref and not _SHA256_REF_RE.match(repair_context_ref):
        raise RuntimeOperationTransitionError("repair_context_ref must be sha256 digest")
    driver = _text(payload.get("driver"), "driver", allow_empty=True)
    _validate_pending_leaf(
        leaf=leaf,
        driver=driver,
        turn=turn,
        tool_index=tool_index,
    )
    return proof_ref, proof_status, satisfied, repair_context_ref, driver

def _require_repair_leaf_consistent(
    *,
    leaf: str,
    driver: str,
    repair_context_ref: str,
    proof_status: str,
    repair_rounds: int,
) -> bool:
    repair_leaf = _is_repair_leaf(leaf, driver)
    if repair_leaf:
        if not repair_context_ref:
            raise RuntimeOperationTransitionError("repair leaf requires context ref")
        if proof_status != "failed":
            raise RuntimeOperationTransitionError("repair leaf requires failed proof")
    if repair_leaf and leaf != LEAF_REPAIR_CONTEXT_ADMITTED and repair_rounds < 1:
        raise RuntimeOperationTransitionError("repair execution requires committed round")
    return repair_leaf

def _require_proof_stage_consistent(
    *,
    leaf: str,
    driver: str,
    repair_leaf: bool,
    proof_ref: str,
    proof_status: str,
    satisfied: object,
    repair_rounds: int,
    repair_context_ref: str,
) -> None:
    if leaf in _PRE_REPAIR_LEAVES or _is_writer_pending_leaf(leaf, driver):
        if _repair_facts_claimed(repair_rounds, repair_context_ref):
            raise RuntimeOperationTransitionError("pre-repair leaf cannot carry repair facts")
        if _proof_facts_claimed(proof_ref, proof_status, satisfied):
            raise RuntimeOperationTransitionError("pre-repair leaf cannot carry proof facts")
    elif leaf in _POST_PROOF_BASE_LEAVES or repair_leaf:
        if not _proof_facts_complete(proof_ref, proof_status, satisfied):
            raise RuntimeOperationTransitionError("post-proof leaf requires proof facts")
        if leaf == LEAF_COMPLETION_PROOF_RECORDED and _repair_facts_claimed(
            repair_rounds,
            repair_context_ref,
        ) and (not repair_context_ref or repair_rounds < 1):
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

def _parse_operation_verdict(
    payload: dict[str, object],
    *,
    leaf: str,
    proof_ref: str,
    proof_status: str,
    satisfied: object,
) -> str:
    blocked_reason = _text(payload.get("blocked_reason"), "blocked_reason", allow_empty=True)
    if blocked_reason and leaf not in _VERDICT_LEAVES:
        raise RuntimeOperationTransitionError("blocked verdict is not valid for leaf")
    if blocked_reason and not _blocked_verdict_facts_supported(
        proof_ref,
        proof_status,
        satisfied,
    ):
        raise RuntimeOperationTransitionError("blocked verdict requires failed or blocked proof")
    return blocked_reason

def _require_fresh_leaf_facts(
    *,
    leaf: str,
    writer_attempt: int,
    turns_used: int,
    stop_reason: str,
) -> None:
    if leaf == LEAF_ACCEPTED and (writer_attempt != 1 or turns_used or stop_reason):
        raise RuntimeOperationTransitionError("accepted must be fresh")
    # A continuation preserves the budget already consumed. It must never
    # carry a settled verdict; the initial attempt still starts at zero.
    if leaf == LEAF_WRITER_RUNNING and (stop_reason or (writer_attempt == 1 and turns_used)):
        raise RuntimeOperationTransitionError("writer_running cannot carry settled facts")

def _parse_operation_terminal(
    payload: dict[str, object],
    *,
    leaf: str,
    turn_budget: int,
    blocked_reason: str,
) -> RuntimeOperationTerminal | None:
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
    return terminal

def _validate_pending_leaf(
    *,
    leaf: str,
    driver: str,
    turn: int,
    tool_index: int,
) -> None:
    if leaf not in _PENDING_LEAVES:
        if driver or turn or tool_index:
            raise RuntimeOperationTransitionError("non-pending leaf carries pending effect facts")
        return
    if driver not in DRIVERS:
        raise RuntimeOperationTransitionError("pending leaf requires driver")
    if leaf in {LEAF_PROVIDER_REQUEST_READY, LEAF_PROVIDER_EFFECT_PENDING, LEAF_PROVIDER_RESPONSE_READY} and tool_index:
        raise RuntimeOperationTransitionError("provider pending cannot carry tool index")

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
