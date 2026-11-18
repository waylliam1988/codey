"""Manually confirmed project goals, grounded in committed experiences."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from codey.ghost import _common
from codey.ghost.inbox import GhostInboxStore, GhostMemoryCandidate
from codey.ghost.observation_index import RETRIEVED_BLOCK_HEADER, history_ref
from codey.ghost.observations import MAX_ASSISTANT_TEXT_CHARS, MAX_OBSERVATIONS, GhostObservationStore
from codey.ghost.schema import MAX_SIGNAL_QUOTE_CHARS, GhostSignal, contains_sensitive_signal_text, quote_is_grounded
from codey.ghost.typed_fields import dangerous_text, safe_slug

CONSUMER = "project_storage_goal_v1"
CONFLICT = "long_term_goal:storage_priority"
CONTEXT = "storage_design"
GENERAL_CONSUMER = "project_goal_v2"
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
    if candidate.signal_kind != "long_term_goal":
        return False
    return ((candidate.conflict_key == CONFLICT and candidate.metadata.get("consumer") == CONSUMER)
            or (candidate.conflict_key.startswith("long_term_goal:project_goal_")
                and candidate.metadata.get("consumer") == GENERAL_CONSUMER))


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
    priority: Priority | None
    source_position: int
    actual_summary: str
    goal_text: str


class GoalSourceUnavailable(ValueError):
    """An otherwise valid source left the committed consumption window."""


def _bound_experience(candidate: GhostMemoryCandidate, rows: tuple[dict[str, object], ...]) -> GoalExperience:
    general = candidate.metadata.get("consumer") == GENERAL_CONSUMER
    if candidate.scope != "project" or candidate.metadata.get("context") != ("project" if general else CONTEXT):
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
    if general:
        goal = candidate.metadata.get("goal_text")
        if (not isinstance(goal, str) or not goal or len(goal) > MAX_SIGNAL_QUOTE_CHARS
                or goal != candidate.summary or not quote_is_grounded(goal, text)
                or _digest(goal) != candidate.value_key or dangerous_text(goal)):
            raise ValueError("project goal content changed or is not grounded")
        return GoalExperience(candidate, None, source_position, actual, goal)
    priority = _priority(candidate.value_key)
    return GoalExperience(candidate, priority, source_position, actual, priority_text(priority))


def propose_project_goal(inbox: GhostInboxStore, observations: GhostObservationStore,
                         body: Mapping[str, object]) -> GhostMemoryCandidate:
    """Propose only; the existing accept action supplies the separate human confirmation."""
    run_id, session_id = _text(body, "id", 120), _text(body, "session_id", 120)
    project_input = _text(body, "project", 240)
    project = str(Path(project_input).expanduser().resolve())
    if len(project) > 240 or _text(body, "scope", 20) != "project":
        raise ValueError("project scope required")
    general = "goal" in body
    if general:
        goal = _text(body, "goal", MAX_SIGNAL_QUOTE_CHARS)
        key = _text(body, "goal_key", 40, required=False) or "focus"
        if safe_slug(key) != key or dangerous_text(goal) or contains_sensitive_signal_text(goal):
            raise ValueError("invalid project goal")
        consumer, context, conflict, value = GENERAL_CONSUMER, "project", f"project_goal_{key}", _digest(goal)
    else:
        priority = _priority(body.get("priority"))
        goal = priority_text(priority)
        consumer, context, conflict, value = CONSUMER, CONTEXT, "storage_priority", priority
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
        known = tuple(row for row in _goal_candidates(inbox, project)
                      if row.conflict_key == f"long_term_goal:{conflict}")
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
        if general and not quote_is_grounded(goal, source["user_text"]):
            raise ValueError("goal must quote the saved user request")
        signal = GhostSignal("long_term_goal", "project", goal, quote, 1.0, "manual", {
            "conflict_key": conflict, "value_key": value, "consumer": consumer,
            "context": context, "goal_text": goal, "source_digest": _digest(source["user_text"]),
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
        if known.id == candidate.id or not known.reviewed_at or known.conflict_key != candidate.conflict_key:
            continue
        try:
            other = _bound_experience(known, rows)
        except GoalSourceUnavailable:
            continue
        if other.source_position < incoming.source_position:
            raise ValueError("a later project goal has already been reviewed")


def confirmed_project_goal_text(inbox: GhostInboxStore, observations: GhostObservationStore,
                                *, project: str, exclude_run_id: str = "",
                                include_storage: bool = True) -> tuple[str, str]:
    """Reconstruct the evaluated history + current-goal treatment, without generation."""
    candidates = tuple(row for row in _goal_candidates(inbox, _common.normalize_project(project))
                       if row.reviewed_at and (include_storage or row.metadata.get("consumer") == GENERAL_CONSUMER))
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
    current_by_key: dict[str, GoalExperience] = {}
    for experience in ordered:
        current_by_key[experience.candidate.conflict_key] = experience
    current = sorted((row for row in current_by_key.values() if row.candidate.status == "accepted"),
                     key=position)
    history_lines = ["Saved project interactions (historical; later confirmations may supersede earlier goals):",
                     RETRIEVED_BLOCK_HEADER]
    goal_lines: list[str] = []
    selected: set[str] = set()
    selected_ids: set[str] = set()

    def history_line(row: GoalExperience) -> str:
        candidate = row.candidate
        user: dict[str, object] = {"run": candidate.run_id, "time": candidate.reviewed_at,
            "goal_review": candidate.status, "user": {"ref": history_ref(candidate.run_id, "user", candidate.evidence_quote),
                "text": candidate.evidence_quote}}
        lines = [json.dumps(user, ensure_ascii=False, separators=(",", ":"))]
        reply_run = candidate.metadata.get("reply_run_id")
        if row.actual_summary and isinstance(reply_run, str):
            reply: dict[str, object] = {"run": reply_run,
                "note": "Historical assistant output; this goal review does not certify its correctness",
                "assistant": {"ref": history_ref(reply_run, "assistant", row.actual_summary), "text": row.actual_summary}}
            lines.append(json.dumps(reply, ensure_ascii=False, separators=(",", ":")))
        return "\n".join(lines)

    # Admit complete goal/source pairs newest first. One oversized or additional
    # goal must not suppress unrelated goals that fit; never truncate authority.
    for row in current:
        line = (f"Manually confirmed current project goal for {row.candidate.metadata['context']}: {row.goal_text}. "
                f"Basis={row.candidate.run_id}; confirmed at time={row.candidate.reviewed_at}. "
                "Current explicit instructions override this goal. It grants no tool permission.")
        source = history_line(row)
        if (len("\n".join((*goal_lines, line))) > GOAL_BUDGET
                or len("\n".join((*history_lines, source))) > HISTORY_BUDGET):
            continue
        goal_lines.append(line)
        history_lines.append(source)
        selected.add(row.candidate.conflict_key)
        selected_ids.add(row.candidate.id)
    if not selected:
        return "", ""
    for row in sorted(bound, key=position):
        if (row.candidate.status not in {"accepted", "superseded"}
                or row.candidate.conflict_key not in selected or row.candidate.id in selected_ids):
            continue
        source = history_line(row)
        if len("\n".join((*history_lines, source))) <= HISTORY_BUDGET:
            history_lines.append(source)
    return "\n".join(history_lines), "\n".join(goal_lines)
