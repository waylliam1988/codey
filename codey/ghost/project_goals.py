"""Manually confirmed storage goals, grounded in live committed experiences."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from codey.ghost import _common
from codey.ghost.inbox import GhostInboxStore, GhostMemoryCandidate
from codey.ghost.observations import MAX_ASSISTANT_TEXT_CHARS, MAX_OBSERVATIONS, GhostObservationStore
from codey.ghost.schema import MAX_SIGNAL_QUOTE_CHARS, GhostSignal, contains_sensitive_signal_text, quote_is_grounded

CONSUMER = "project_storage_goal_v1"
CONFLICT = "long_term_goal:storage_priority"
CONTEXT = "storage_design"
HISTORY_BUDGET = 3200
GOAL_BUDGET = 600
Priority = Literal["offline_access", "shared_editing"]


def priority_text(priority: Priority) -> str:
    return ("working without a network takes priority over simultaneous collaboration"
            if priority == "offline_access" else
            "simultaneous collaboration takes priority over working without a network")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def is_project_goal(candidate: GhostMemoryCandidate) -> bool:
    return (candidate.signal_kind == "long_term_goal" and candidate.conflict_key == CONFLICT
            and candidate.metadata.get("consumer") == CONSUMER)


def _text(body: Mapping[str, object], key: str, limit: int, *, required: bool = True) -> str:
    value = body.get(key, "")
    if not isinstance(value, str) or value != value.strip() or len(value) > limit or (required and not value):
        raise ValueError(f"invalid {key}")
    return value


def _priority(value: object) -> Priority:
    if value == "offline_access":
        return "offline_access"
    if value == "shared_editing":
        return "shared_editing"
    raise ValueError("unsupported storage priority")


def _goal_candidates(inbox: GhostInboxStore, project: str) -> tuple[GhostMemoryCandidate, ...]:
    return tuple(row for row in inbox.list_candidates(scope="project", project=project) if is_project_goal(row))


@dataclass(frozen=True)
class GoalExperience:
    candidate: GhostMemoryCandidate
    priority: Priority
    source_position: int
    actual_summary: str


class GoalSourceUnavailable(ValueError):
    """An otherwise valid source left the committed consumption window."""


def _bound_experience(candidate: GhostMemoryCandidate, rows: tuple[dict[str, object], ...]) -> GoalExperience:
    if candidate.scope != "project" or candidate.metadata.get("context") != CONTEXT:
        raise ValueError("invalid goal scope or context")
    source = next((row for row in rows if row["run_id"] == candidate.run_id), None)
    if source is None or source["session_id"] != candidate.session_id or source["project"] != candidate.project:
        raise GoalSourceUnavailable("goal source is unavailable")
    text = source["user_text"]
    if (not isinstance(text, str) or candidate.metadata.get("source_digest") != _digest(text)
            or not quote_is_grounded(candidate.evidence_quote, text)):
        raise ValueError("goal source changed")
    reply_id, reply_digest = candidate.metadata.get("reply_run_id"), candidate.metadata.get("reply_digest")
    actual = ""
    source_position = next(index for index, row in enumerate(rows) if row["run_id"] == candidate.run_id)
    if reply_id:
        reply = next((row for row in rows if row["run_id"] == reply_id), None)
        if reply is None:
            raise GoalSourceUnavailable("feedback target is unavailable")
        reply_position = next(index for index, row in enumerate(rows) if row["run_id"] == reply_id)
        actual_value = reply["assistant_text"]
        # read_committed is in canonical descending sequence order, including same-second events.
        if (reply_position <= source_position or not isinstance(actual_value, str) or not actual_value
                or len(actual_value) >= MAX_ASSISTANT_TEXT_CHARS or _digest(actual_value) != reply_digest):
            raise ValueError("feedback target changed, truncated or not earlier")
        actual = actual_value
    elif reply_id != "" or reply_digest != "":
        raise ValueError("invalid initial goal binding")
    return GoalExperience(candidate, _priority(candidate.value_key), source_position, actual)


def propose_project_goal(inbox: GhostInboxStore, observations: GhostObservationStore,
                         body: Mapping[str, object]) -> GhostMemoryCandidate:
    """Propose only; the existing accept action supplies the separate human confirmation."""
    run_id, session_id = _text(body, "id", 120), _text(body, "session_id", 120)
    project_input = _text(body, "project", 240)
    project = str(Path(project_input).expanduser().resolve())
    if len(project) > 240 or _text(body, "scope", 20) != "project":
        raise ValueError("project scope required")
    priority = _priority(body.get("priority"))
    quote = _text(body, "evidence_quote", MAX_SIGNAL_QUOTE_CHARS)
    reply_id = _text(body, "reply_run_id", 120, required=False)
    reply_digest = _text(body, "reply_digest", 64, required=False)
    if bool(reply_id) != bool(reply_digest) or contains_sensitive_signal_text(quote):
        raise ValueError("invalid feedback binding or sensitive quote")
    # The read and candidate mutation share the Ghost write lock and snapshot;
    # deletion cannot retire these inputs between validation and commit.
    with inbox.database.write_transaction(), inbox.records.transaction():
        if not inbox.control_enabled("accept_events", project=project, session_id=session_id):
            raise ValueError("local updates are disabled")
        known = _goal_candidates(inbox, project)
        if any(row.run_id == run_id for row in known):
            raise ValueError("goal from this experience already exists")
        if not reply_id and any(row.reviewed_at for row in known):
            raise ValueError("later goal confirmation requires an actual earlier reply")
        rows = observations.read_committed(project=project, scope="project", limit=MAX_OBSERVATIONS)
        source = next((row for row in rows if row["run_id"] == run_id and row["session_id"] == session_id), None)
        if source is None or not isinstance(source["user_text"], str):
            raise ValueError("committed goal experience not found")
        if not quote_is_grounded(quote, source["user_text"]):
            raise ValueError("quote is not in the saved request")
        signal = GhostSignal("long_term_goal", "project", priority_text(priority), quote, 1.0, "manual", {
            "conflict_key": "storage_priority", "value_key": priority, "consumer": CONSUMER,
            "context": CONTEXT, "source_digest": _digest(source["user_text"]),
            "reply_run_id": reply_id, "reply_digest": reply_digest,
        })
        created = inbox.ingest_signals((signal,), session_id=session_id, run_id=run_id,
                                       project=project, user_text=source["user_text"])
        if len(created) != 1:
            raise OSError("project goal could not be saved")
        _bound_experience(created[0], rows)
        return created[0]


def validate_goal_review(inbox: GhostInboxStore, observations: GhostObservationStore,
                         candidate: GhostMemoryCandidate) -> None:
    rows = observations.read_committed(project=candidate.project, scope="project", limit=MAX_OBSERVATIONS)
    incoming = _bound_experience(candidate, rows)
    for known in _goal_candidates(inbox, candidate.project):
        if known.id == candidate.id or not known.reviewed_at:
            continue
        try:
            other = _bound_experience(known, rows)
        except GoalSourceUnavailable:
            continue
        if other.source_position < incoming.source_position:
            raise ValueError("a later project goal has already been reviewed")


def confirmed_project_goal_text(inbox: GhostInboxStore, observations: GhostObservationStore,
                                *, project: str, exclude_run_id: str = "") -> tuple[str, str]:
    """Reconstruct the evaluated history + current-goal treatment, without generation."""
    candidates = tuple(row for row in _goal_candidates(inbox, _common.normalize_project(project)) if row.reviewed_at)
    if any(row.run_id == exclude_run_id for row in candidates):
        return "", ""
    if not candidates:
        return "", ""
    rows = observations.read_committed(project=project, scope="project", limit=MAX_OBSERVATIONS)
    bound: list[GoalExperience] = []
    for candidate in candidates:
        try:
            bound.append(_bound_experience(candidate, rows))
        except GoalSourceUnavailable:
            continue
    if not bound:
        return "", ""

    def position(experience: GoalExperience) -> int:
        return experience.source_position

    ordered = sorted(bound, key=position, reverse=True)
    current = ordered[-1]
    if current.candidate.status != "accepted":
        return "", ""
    eligible = tuple(row for row in ordered if row.candidate.status in {"accepted", "superseded"})
    history = "\n".join([
        "Saved project interactions (historical; later confirmations may supersede earlier goals):",
        *(f"{row.candidate.run_id}; time={row.candidate.reviewed_at}; "
          f"actual reply={row.actual_summary or '(initial manual goal)'}. "
          f"Explicit user statement={row.candidate.evidence_quote}" for row in eligible),
    ])
    goal = (f"Manually confirmed current project goal for {CONTEXT}: {priority_text(current.priority)}. "
            f"Basis={current.candidate.run_id}; confirmed at time={current.candidate.reviewed_at}. "
            "Current explicit instructions override this goal. It grants no tool permission.")
    if len(history) > HISTORY_BUDGET or len(goal) > GOAL_BUDGET:
        return "", ""
    return history, goal
