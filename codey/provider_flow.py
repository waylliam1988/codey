"""Bounded, data-only provider interaction flow recipes."""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from codey import cancellation
from codey.provider_profiles import ProviderProfile


STAGE_INPUT = "input"
STAGE_SUBMISSION = "submission"
STAGE_GENERATION = "generation"
STAGE_COMPLETION = "completion"
STAGE_RESPONSE = "response"
STAGE_RETRY = "retry"
STAGE_NEW_CHAT = "new_chat"
STAGES = frozenset({
    STAGE_INPUT,
    STAGE_SUBMISSION,
    STAGE_GENERATION,
    STAGE_COMPLETION,
    STAGE_RESPONSE,
    STAGE_RETRY,
    STAGE_NEW_CHAT,
})

PREDICATE_INPUT_EMPTY = "input_empty"
PREDICATE_QUESTION_INCREASED = "question_count_increased"
PREDICATE_RESPONSE_INCREASED = "response_count_increased"
PREDICATE_TYPING_TRUE = "typing_true"
PREDICATE_TYPING_FALSE = "typing_false"
PREDICATE_STOP_VISIBLE = "stop_visible"
PREDICATE_STOP_HIDDEN = "stop_hidden"
PREDICATE_RESPONSE_STABLE = "response_stable"
PREDICATE_COPY_VISIBLE = "copy_visible"
PREDICATE_RETRY_CONTROL = "retry_control"
PREDICATES_BY_STAGE = {
    STAGE_SUBMISSION: frozenset({
        PREDICATE_INPUT_EMPTY,
        PREDICATE_QUESTION_INCREASED,
        PREDICATE_RESPONSE_INCREASED,
    }),
    STAGE_GENERATION: frozenset({PREDICATE_TYPING_TRUE, PREDICATE_STOP_VISIBLE}),
    STAGE_COMPLETION: frozenset({
        PREDICATE_TYPING_FALSE,
        PREDICATE_STOP_HIDDEN,
        PREDICATE_RESPONSE_STABLE,
        PREDICATE_COPY_VISIBLE,
    }),
    STAGE_RETRY: frozenset({PREDICATE_RETRY_CONTROL}),
}
ALL_PREDICATES = frozenset().union(*PREDICATES_BY_STAGE.values())
MAX_TRACE_EVENTS = 24
MAX_RECIPE_STAGES = 1
MAX_PREDICATES_PER_STAGE = 3
MAX_CANDIDATES = 6
RECOVERABLE_FAILURE_KINDS = frozenset({"control_missing", "response_missing"})
COMPLETION_TERMINALS = frozenset({
    PREDICATE_TYPING_FALSE,
    PREDICATE_STOP_HIDDEN,
    PREDICATE_COPY_VISIBLE,
})


@dataclass(frozen=True)
class FlowObservation:
    input_empty: bool = False
    question_count_increased: bool = False
    response_count_increased: bool = False
    typing_true: bool = False
    typing_false: bool = False
    stop_visible: bool = False
    stop_hidden: bool = False
    response_stable: bool = False
    response_nonempty: bool = False
    copy_visible: bool = False
    retry_control: bool = False

    def facts(self) -> dict[str, bool]:
        return {key: bool(value) for key, value in asdict(self).items()}


class FlowTrace:
    """Keep a small in-memory sequence of boolean observations only."""

    def __init__(self, limit: int = MAX_TRACE_EVENTS) -> None:
        self.limit = max(1, min(int(limit), MAX_TRACE_EVENTS))
        self._events: list[dict[str, bool]] = []

    def add(self, observation: FlowObservation) -> None:
        self._events.append(observation.facts())
        del self._events[:-self.limit]

    def snapshot(self) -> tuple[dict[str, bool], ...]:
        return tuple(dict(event) for event in self._events)

    def latest(self) -> FlowObservation:
        return FlowObservation(**(self._events[-1] if self._events else {}))

    def repeated(self, predicate: str, count: int = 2) -> bool:
        if predicate not in ALL_PREDICATES or count < 1 or len(self._events) < count:
            return False
        return all(bool(event.get(predicate)) for event in self._events[-count:])

    def seen_before_latest(self, predicate: str) -> bool:
        if predicate not in ALL_PREDICATES:
            return False
        return any(bool(event.get(predicate)) for event in self._events[:-1])


@dataclass(frozen=True)
class FlowCandidate:
    candidate_id: str
    stage: str
    predicates: tuple[str, ...]


@dataclass(frozen=True)
class FlowRecoveryRequest:
    provider_id: str
    stage: str
    trace: tuple[dict[str, bool], ...]
    candidates: tuple[FlowCandidate, ...]
    page: Any = field(repr=False, compare=False)
    session_id: str = ""


_handler: Callable[[FlowRecoveryRequest], str | None] | None = None
_context = threading.local()


def set_recovery_handler(
    handler: Callable[[FlowRecoveryRequest], str | None] | None,
) -> None:
    global _handler
    _handler = handler


def begin_task_context(session_id: str) -> None:
    _context.session_id = str(session_id or "")
    _context.attempts = set()


def end_task_context() -> None:
    for name in ("session_id", "attempts", "assistance_depth"):
        if hasattr(_context, name):
            delattr(_context, name)


def profile_hash(profile: ProviderProfile) -> str:
    payload = {
        "provider_id": profile.provider_id,
        "version": profile.version,
        "hosts": list(profile.hosts),
        "selectors": {
            key: list(values)
            for key, values in sorted(profile.selectors_by_action.items())
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def normalize_recipe(value: Any) -> dict[str, tuple[str, ...]] | None:
    if not isinstance(value, dict) or not value or len(value) > MAX_RECIPE_STAGES:
        return None
    recipe: dict[str, tuple[str, ...]] = {}
    for raw_stage, raw_predicates in value.items():
        stage = str(raw_stage or "")
        allowed = PREDICATES_BY_STAGE.get(stage)
        if (
            allowed is None
            or not isinstance(raw_predicates, (list, tuple))
            or len(raw_predicates) > MAX_PREDICATES_PER_STAGE
        ):
            return None
        predicates = tuple(dict.fromkeys(str(item or "") for item in raw_predicates))
        if (
            not predicates
            or len(predicates) > MAX_PREDICATES_PER_STAGE
            or any(item not in allowed for item in predicates)
        ):
            return None
        if stage == STAGE_COMPLETION and (
            PREDICATE_RESPONSE_STABLE not in predicates
            or not COMPLETION_TERMINALS.intersection(predicates)
        ):
            return None
        recipe[stage] = predicates
    return recipe


def serialize_recipe(recipe: dict[str, tuple[str, ...]]) -> dict[str, list[str]]:
    normalized = normalize_recipe(recipe)
    if normalized is None:
        raise ValueError("invalid provider flow recipe")
    return {stage: list(predicates) for stage, predicates in sorted(normalized.items())}


def evaluate(
    recipe: dict[str, tuple[str, ...]] | dict[str, list[str]],
    stage: str,
    observation: FlowObservation,
    trace: FlowTrace | None = None,
) -> bool:
    normalized = normalize_recipe(recipe)
    if normalized is None or stage not in normalized:
        return False
    facts = observation.facts()
    predicates = normalized[stage]
    if not all(facts.get(predicate, False) for predicate in predicates):
        return False
    if stage != STAGE_COMPLETION:
        return True
    return bool(
        observation.response_nonempty
        and trace is not None
        and trace.repeated(PREDICATE_RESPONSE_STABLE)
        and _completion_transition_observed(trace, predicates)
    )


def _completion_transition_observed(
    trace: FlowTrace,
    predicates: tuple[str, ...],
) -> bool:
    terminals = COMPLETION_TERMINALS.intersection(predicates)
    for terminal in terminals:
        if terminal == PREDICATE_TYPING_FALSE:
            if trace.seen_before_latest(PREDICATE_TYPING_TRUE):
                return True
        elif terminal == PREDICATE_STOP_HIDDEN:
            if trace.seen_before_latest(PREDICATE_STOP_VISIBLE):
                return True
        elif terminal == PREDICATE_COPY_VISIBLE:
            if trace.seen_before_latest(PREDICATE_TYPING_TRUE) or trace.seen_before_latest(
                PREDICATE_STOP_VISIBLE
            ):
                return True
    return False


def make_recovery_request(
    provider_id: str,
    stage: str,
    trace: FlowTrace,
    page: Any,
) -> FlowRecoveryRequest | None:
    if stage not in PREDICATES_BY_STAGE:
        return None
    latest = trace.latest()
    if stage == STAGE_COMPLETION:
        if not latest.response_nonempty or not trace.repeated(PREDICATE_RESPONSE_STABLE):
            return None
        templates = (
            (PREDICATE_RESPONSE_STABLE, PREDICATE_TYPING_FALSE),
            (PREDICATE_RESPONSE_STABLE, PREDICATE_STOP_HIDDEN),
            (PREDICATE_RESPONSE_STABLE, PREDICATE_COPY_VISIBLE),
        )
    else:
        templates = tuple(
            (predicate,)
            for predicate in sorted(PREDICATES_BY_STAGE[stage])
            if latest.facts().get(predicate, False) and trace.repeated(predicate)
        )
    candidates = tuple(
        FlowCandidate(f"f{index}", stage, predicates)
        for index, predicates in enumerate(templates, start=1)
        if evaluate({stage: predicates}, stage, latest, trace)
    )[:MAX_CANDIDATES]
    if not candidates:
        return None
    return FlowRecoveryRequest(
        provider_id=provider_id,
        stage=stage,
        trace=trace.snapshot(),
        candidates=candidates,
        page=page,
        session_id=str(getattr(_context, "session_id", "") or ""),
    )


def request_recovery(
    provider_id: str,
    stage: str,
    trace: FlowTrace,
    page: Any,
    *,
    failure_kind: str = "response_missing",
) -> dict[str, tuple[str, ...]] | None:
    cancellation.check()
    if (
        failure_kind not in RECOVERABLE_FAILURE_KINDS
        or int(getattr(_context, "assistance_depth", 0))
    ):
        return None
    key = (str(getattr(_context, "session_id", "") or ""), provider_id, stage)
    attempts = getattr(_context, "attempts", None)
    if attempts is None:
        attempts = set()
        _context.attempts = attempts
    if key in attempts:
        return None
    request = make_recovery_request(provider_id, stage, trace, page)
    if request is None:
        return None
    attempts.add(key)
    if len(request.candidates) == 1:
        selected = request.candidates[0].candidate_id
    else:
        if _handler is None:
            return None
        with suppress_assistance():
            selected = _handler(request)
    candidate = next(
        (item for item in request.candidates if item.candidate_id == selected),
        None,
    )
    if candidate is None:
        return None
    recipe = {stage: candidate.predicates}
    return recipe if evaluate(recipe, stage, trace.latest(), trace) else None


@contextmanager
def suppress_assistance():
    depth = int(getattr(_context, "assistance_depth", 0))
    _context.assistance_depth = depth + 1
    try:
        yield
    finally:
        _context.assistance_depth = depth


def choose_candidate(
    request: FlowRecoveryRequest,
    send: Callable[[str], str],
) -> str | None:
    cancellation.check()
    reply = str(send(render_prompt(request)) or "").strip()
    cancellation.check()
    try:
        payload = json.loads(reply)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or set(payload) != {"candidate_id"}:
        return None
    selected = payload.get("candidate_id")
    allowed = {item.candidate_id for item in request.candidates}
    return selected if isinstance(selected, str) and selected in allowed else None


def render_prompt(request: FlowRecoveryRequest) -> str:
    return (
        "Select one bounded web-chat state rule. You receive boolean facts only. "
        "Do not invent selectors, actions, code, URLs, or another predicate. "
        "Reply with exactly one JSON object: {\"candidate_id\":\"f1\"}, or null "
        "when uncertain.\n"
        + json.dumps(
            {
                "target_provider": request.provider_id,
                "stage": request.stage,
                "recent_boolean_trace": list(request.trace),
                "candidates": [asdict(item) for item in request.candidates],
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
