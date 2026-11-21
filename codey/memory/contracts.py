"""The sole public memory contract. Implementations own no execution state."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from codey.types.json_value import JsonObject, JsonValue

MemoryKind = Literal["experience", "preference", "goal", "hypothesis"]
MemoryControlName = Literal["accept_events", "retrieve_projections", "run_maintenance", "admit_hints"]
MemoryState = Literal["active", "superseded", "revoked", "quarantined", "expired"]
FeedbackRating = Literal["helpful", "unhelpful", "correction"]
PredictionOutcome = Literal["supported", "contradicted", "unjudged"]
MemoryFamilyState = Literal["observed", "shadow_candidate", "production_candidate", "enabled", "demoted", "quarantined", "deleted"]
MemoryRelation = Literal["equivalent", "conflicting", "context_exception", "suspected_conflict"]
MEMORY_FAMILY_STATES: tuple[MemoryFamilyState, ...] = (
    "observed", "shadow_candidate", "production_candidate", "enabled", "demoted", "quarantined", "deleted",
)
MEMORY_CANDIDATE_LIMIT = 4
MEMORY_CANDIDATE_TEXT_LIMIT = 1200
MEMORY_EXPERIENCE_EXCERPT_CHARS = 2000
MEMORY_POLICY_VERSION = "memory-v3"
MEMORY_CONTROLS: tuple[MemoryControlName, ...] = (
    "accept_events", "retrieve_projections", "run_maintenance", "admit_hints",
)


@dataclass(frozen=True)
class MemoryItem:
    item_id: str
    kind: MemoryKind
    text: str
    scope: str
    source_refs: tuple[str, ...]
    created_at: str
    expires_at: str = ""
    confidence: float | None = None
    slot: str = ""
    context: str = ""
    supersedes: tuple[str, ...] = ()
    value: str = ""

    def to_payload(self) -> JsonObject:
        return {
            "item_id": self.item_id, "kind": self.kind, "text": self.text,
            "scope": self.scope, "source_refs": list(self.source_refs),
            "created_at": self.created_at, "expires_at": self.expires_at,
            "confidence": self.confidence,
            "slot": self.slot, "context": self.context, "supersedes": list(self.supersedes),
            "value": self.value,
        }


@dataclass(frozen=True)
class MemoryQuery:
    text: str
    scopes: tuple[str, ...]
    as_of: str
    exclude_run_id: str = ""
    max_items: int = 8
    control_scopes: tuple[str, ...] = ()
    context: str = ""
    shadow: bool = False


@dataclass(frozen=True)
class MemoryTrace:
    item_id: str
    reason: str
    score: float = 0.0
    rank: int = 0
    chars: int = 0
    scope: str = ""
    source_refs: tuple[str, ...] = ()
    family: str = ""
    item_version: str = ""
    family_state: str = ""
    policy_version: str = MEMORY_POLICY_VERSION
    budget_unit: str = "characters"
    displaced_item_ids: tuple[str, ...] = ()
    conflict_refs: tuple[str, ...] = ()
    consumer: str = ""

    def to_payload(self) -> JsonObject:
        return {"item_id": self.item_id, "reason": self.reason, "score": self.score,
                "rank": self.rank, "chars": self.chars, "scope": self.scope,
                "source_refs": list(self.source_refs), "family": self.family,
                "item_version": self.item_version, "family_state": self.family_state,
                "policy_version": self.policy_version, "budget_unit": self.budget_unit,
                "displaced_item_ids": list(self.displaced_item_ids), "conflict_refs": list(self.conflict_refs), "consumer": self.consumer}


@dataclass(frozen=True)
class MemoryCandidateDraft:
    kind: MemoryKind
    text: str
    quote: str
    quote_start: int
    quote_end: int
    slot: str = ""
    value: str = ""
    context: str = ""
    expires_at: str = ""
    related_item_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryExposure:
    exposure_id: str
    item_id: str
    item_version: str
    run_id: str
    request_ref: str
    scope: str
    occurred_at: str
    context: str = ""

    def to_payload(self) -> JsonObject:
        return {"id": self.exposure_id, "item_id": self.item_id, "item_version": self.item_version,
                "run_id": self.run_id, "source_ref": self.request_ref, "scope": self.scope, "at": self.occurred_at,
                "context": self.context}


@dataclass(frozen=True)
class MemoryBehavior:
    behavior_id: str
    run_id: str
    request_ref: str
    behavior_kind: str
    value: str
    observation_ref: str
    workspace_revision: str = ""

    def to_payload(self) -> JsonObject:
        return {"id": self.behavior_id, "run_id": self.run_id, "request_ref": self.request_ref,
                "kind": self.behavior_kind, "value": self.value, "observation_ref": self.observation_ref,
                "workspace_revision": self.workspace_revision}


@dataclass(frozen=True)
class MemoryFamilyChange:
    family: str
    consumer: str
    scope: str
    state: MemoryFamilyState
    source_ref: str
    occurred_at: str
    evaluation_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryEvaluation:
    evaluation_id: str
    family: str
    consumer: str
    protocol_digest: str
    dataset_digest: str
    source_digest: str
    control_passed: int
    treatment_passed: int
    pairs: int
    requests: int
    responses: int
    boundary_violations: int
    costs_accepted: bool

    def to_payload(self) -> JsonObject:
        return {"id": self.evaluation_id, "family": self.family, "consumer": self.consumer,
            "protocol_digest": self.protocol_digest, "dataset_digest": self.dataset_digest, "source_digest": self.source_digest,
            "control_passed": self.control_passed, "treatment_passed": self.treatment_passed, "pairs": self.pairs,
            "requests": self.requests, "responses": self.responses, "boundary_violations": self.boundary_violations,
            "costs_accepted": self.costs_accepted}


@dataclass(frozen=True)
class MemoryMaintenanceRequest:
    operation_id: str
    as_of: str
    max_items: int = 100
    cursor: str = ""
    policy_version: str = MEMORY_POLICY_VERSION


@dataclass(frozen=True)
class MemoryMaintenanceResult:
    processed: int
    cursor: str
    complete: bool


@dataclass(frozen=True)
class MemoryRetentionPolicy:
    max_experiences_per_scope: int = 1000
    experience_days: int = 180
    archived_days: int = 30
    keep_events: int = 1000

    def __post_init__(self) -> None:
        for value, maximum in ((self.max_experiences_per_scope, 1000000), (self.experience_days, 36500),
                               (self.archived_days, 36500), (self.keep_events, 100000)):
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError("memory retention limit is outside supported bounds")


@dataclass(frozen=True)
class MemoryResult:
    items: tuple[MemoryItem, ...] = ()
    traces: tuple[MemoryTrace, ...] = ()


@dataclass(frozen=True)
class MemoryEvent:
    event_id: str
    kind: str
    occurred_at: str
    session_id: str
    project: str
    run_id: str
    scope: str
    payload: Mapping[str, JsonValue]
    provenance: tuple[str, ...]
    control_scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryProposal:
    proposal_id: str
    item: MemoryItem
    version: str
    relations: tuple[MemoryConflict, ...] = ()


@dataclass(frozen=True)
class MemoryConflict:
    target_id: str
    relation: MemoryRelation
    source_refs: tuple[str, ...]

    def to_payload(self) -> JsonObject:
        return {"target_id": self.target_id, "relation": self.relation, "source_refs": list(self.source_refs)}


@dataclass(frozen=True)
class MemoryDecision:
    proposal_id: str
    version: str
    accepted: bool
    source_ref: str
    decided_at: str
    supersedes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeleteScopeRequest:
    scope: str
    source_refs: tuple[str, ...] = ()
    requested_at: str = ""


@dataclass(frozen=True)
class DeleteResult:
    deleted: int = 0


@dataclass(frozen=True)
class MemoryPolicy:
    accept_events: bool = True
    retrieve_projections: bool = True
    run_maintenance: bool = True
    admit_hints: bool = True

    def permits(self, name: MemoryControlName) -> bool:
        return self.to_payload()[name] is True

    def to_payload(self) -> JsonObject:
        return {"accept_events": self.accept_events, "retrieve_projections": self.retrieve_projections,
                "run_maintenance": self.run_maintenance, "admit_hints": self.admit_hints}


@dataclass(frozen=True)
class MemoryControl:
    scope: str
    name: MemoryControlName
    enabled: bool
    source_ref: str
    occurred_at: str


@dataclass(frozen=True)
class MemoryChange:
    item_id: str
    scope: str
    state: Literal["revoked", "quarantined", "active"]
    source_ref: str
    occurred_at: str


@dataclass(frozen=True)
class MemoryRecord:
    item: MemoryItem
    state: MemoryState = "active"
    superseded_by: str = ""
    control_scopes: tuple[str, ...] = ()
    state_changed_at: str = ""


@dataclass(frozen=True)
class MemoryRebuildResult:
    events: int
    digest: str


@dataclass(frozen=True)
class MemoryFeedback:
    feedback_id: str
    item_id: str
    run_id: str
    scope: str
    rating: FeedbackRating
    source_ref: str
    occurred_at: str
    exposure_id: str = ""
    behavior_id: str = ""


@dataclass(frozen=True)
class MemoryPrediction:
    prediction_id: str
    item_id: str
    scope: str
    statement: str
    expected_rating: FeedbackRating
    source_refs: tuple[str, ...]
    created_at: str
    due_at: str
    target_kind: Literal["feedback", "behavior", "verification"] = "feedback"
    target_key: str = ""
    expected_value: str = ""
    workspace_revision: str = ""


@dataclass(frozen=True)
class MemoryReview:
    prediction_id: str
    feedback_id: str
    outcome: PredictionOutcome
    source_ref: str
    occurred_at: str


class MemoryReader(Protocol):
    def retrieve(self, query: MemoryQuery) -> MemoryResult: ...


class MemoryWriter(Protocol):
    def observe(self, event: MemoryEvent) -> None: ...


class MemoryReviewer(Protocol):
    def proposals(self, scope: str) -> tuple[MemoryProposal, ...]: ...
    def decide(self, decision: MemoryDecision) -> None: ...


class MemoryAdmin(Protocol):
    def delete_scope(self, request: DeleteScopeRequest) -> DeleteResult: ...
    def export_state(self) -> dict[str, JsonValue]: ...


class MemoryMaintainer(Protocol):
    def maintain(self, *, as_of: str) -> None: ...


class MemoryController(Protocol):
    def policy(self, scopes: tuple[str, ...]) -> MemoryPolicy: ...
    def set_control(self, control: MemoryControl) -> None: ...


class MemoryLifecycle(Protocol):
    def change(self, change: MemoryChange) -> None: ...
    def records(self, scopes: tuple[str, ...]) -> tuple[MemoryRecord, ...]: ...
    def rebuild(self) -> MemoryRebuildResult: ...


class MemoryLearning(Protocol):
    def feedback(self, feedback: MemoryFeedback) -> None: ...
    def predict(self, prediction: MemoryPrediction) -> None: ...
    def review(self, review: MemoryReview) -> None: ...


class MemoryGovernance(Protocol):
    def family_change(self, change: MemoryFamilyChange) -> None: ...
    def maintain_batch(self, request: MemoryMaintenanceRequest) -> MemoryMaintenanceResult: ...
    def compact(self, policy: MemoryRetentionPolicy, *, as_of: str) -> MemoryRebuildResult: ...
    def observation_cursor(self) -> int: ...
    def maintenance_cursor(self) -> str: ...


@dataclass(frozen=True)
class MemoryCapabilities:
    reader: MemoryReader
    writer: MemoryWriter
    reviewer: MemoryReviewer
    admin: MemoryAdmin
    maintainer: MemoryMaintainer
    controller: MemoryController | None = None
    lifecycle: MemoryLifecycle | None = None
    learning: MemoryLearning | None = None
    governance: MemoryGovernance | None = None


@dataclass(frozen=True)
class MemoryHost:
    state_dir: Path
    clock: Callable[[], str]
    check_cancelled: Callable[[], None]
