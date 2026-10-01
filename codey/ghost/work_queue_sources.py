"""Ghost work-queue sources: continuity/research/checkpoint conversions.

Owns multi-source work-item generation from already-loaded projections.
Pure conversions: no file locks, no persistence, no model calls. The
work-queue store owns transactions and delegates here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from codey.ghost import _common
from codey.ghost.schema import clip_signal_text
from codey.ghost.work_queue_model import (
    MAX_WORK_REF_CHARS,
    MAX_WORK_TITLE_CHARS,
    MAX_WORK_WHY_CHARS,
    GhostWorkItem,
    _bounded_refs,
    _clean_item_text,
    _clean_kind,
    _clean_metadata,
    _clean_scope,
    _clean_source,
    _clean_status,
    _field,
    _int,
    _list,
    _project_ref,
    _session_ref,
    _stable_item_id,
    _unit_float,
)


def new_item(
    *,
    kind: str,
    status: str,
    scope: str,
    scope_ref: str,
    title: str,
    why_now: str,
    priority: float,
    confidence: float,
    source: str,
    source_ref: str,
    evidence_refs: Iterable[object],
    run_refs: Iterable[object],
    now: str,
    metadata: Mapping[str, object] | None = None,
) -> GhostWorkItem:
    """Create one work item with a stable deterministic id."""
    cleaned_title = _clean_item_text(title, max_chars=MAX_WORK_TITLE_CHARS)
    cleaned_why = _clean_item_text(why_now, max_chars=MAX_WORK_WHY_CHARS)
    clean_kind = _clean_kind(kind) or "project_followup"
    clean_status = _clean_status(status) or "candidate"
    clean_scope = _clean_scope(scope) or "user"
    clean_source = _clean_source(source) or "user"
    clean_source_ref = clip_signal_text(source_ref, MAX_WORK_REF_CHARS)
    clean_scope_ref = clip_signal_text(scope_ref, 240)
    item_id = _stable_item_id(
        kind=clean_kind,
        scope=clean_scope,
        scope_ref=clean_scope_ref,
        source=clean_source,
        source_ref=clean_source_ref,
        title=cleaned_title,
    )
    return GhostWorkItem(
        id=item_id,
        kind=clean_kind,
        status=clean_status,
        scope=clean_scope,
        scope_ref=clean_scope_ref,
        title=cleaned_title,
        why_now=cleaned_why,
        priority=_unit_float(priority),
        confidence=_unit_float(confidence),
        source=clean_source,
        source_ref=clean_source_ref,
        evidence_refs=_bounded_refs(evidence_refs),
        run_refs=_bounded_refs(run_refs),
        created_at=now,
        updated_at=now,
        metadata=_clean_metadata(metadata),
    )


def items_from_continuity(
    store: Any | None,
    *,
    session_id: str,
    project: str,
    now: str,
) -> list[GhostWorkItem]:
    """Convert continuity open questions into work items (stable ids)."""
    if store is None:
        return []
    try:
        rows = store.list_items(project=project, session_id=session_id)
    except Exception:
        return []
    out: list[GhostWorkItem] = []
    for item in rows:
        if item.kind != "open_question":
            continue
        status = "queued" if item.source == "research_note" and item.confidence >= 0.7 else "candidate"
        kind = "research" if item.source == "research_note" else "open_question"
        title = _clean_item_text(item.text, max_chars=MAX_WORK_TITLE_CHARS)
        if not title:
            continue
        scope_ref = item.scope_ref
        if item.scope == "session":
            scope_ref = _session_ref(scope_ref or session_id)
        elif item.scope == "project":
            scope_ref = _common.normalize_project(scope_ref or project)
        else:
            scope_ref = ""
        out.append(
            new_item(
                kind=kind,
                status=status,
                scope=item.scope,
                scope_ref=scope_ref,
                title=title,
                why_now="Open question from bounded local continuity.",
                priority=0.62 if status == "queued" else 0.45,
                confidence=item.confidence,
                source="research_note" if item.source == "research_note" else "continuity",
                source_ref=item.source_ref or item.id,
                evidence_refs=(f"continuity:{item.id}",),
                run_refs=(),
                now=now,
                metadata={"continuity_kind": item.kind, "continuity_source": item.source},
            )
        )
    return out


def items_from_research_interest_candidates(
    candidates: Iterable[Any],
    *,
    session_id: str,
    project: str,
    now: str,
) -> list[GhostWorkItem]:
    """Convert research-interest candidates into work items."""
    out: list[GhostWorkItem] = []
    for candidate in list(candidates or []):
        title = _clean_item_text(_field(candidate, "question"), max_chars=MAX_WORK_TITLE_CHARS)
        if not title:
            continue
        source = _clean_source(_field(candidate, "source")) or "research_interest"
        source_ref = clip_signal_text(_field(candidate, "source_ref") or _field(candidate, "id"), MAX_WORK_REF_CHARS)
        if not source_ref:
            continue
        scope = _clean_scope(_field(candidate, "scope")) or (
            "session" if session_id else "project" if project else "user"
        )
        raw_scope_ref = clip_signal_text(_field(candidate, "scope_ref"), 240)
        if scope == "session":
            scope_ref = _session_ref(raw_scope_ref or session_id)
        elif scope == "project":
            scope_ref = _common.normalize_project(raw_scope_ref or project)
        else:
            scope_ref = ""
        confidence = _unit_float(_field(candidate, "confidence"))
        strong = bool(_field(candidate, "strong_support"))
        if source == "research_note":
            kind = "research"
            status = "queued" if confidence >= 0.7 else "candidate"
            priority = max(0.62, _unit_float(_field(candidate, "priority")))
        elif source == "concept_open_question" and strong and confidence >= 0.72:
            kind = "research"
            status = "queued"
            priority = max(0.66, _unit_float(_field(candidate, "priority")))
        else:
            kind = "open_question"
            status = "candidate"
            priority = max(0.42, _unit_float(_field(candidate, "priority")))
        evidence_refs = _bounded_refs(
            (
                f"research_interest:{clip_signal_text(_field(candidate, 'id'), MAX_WORK_REF_CHARS)}",
                *_list(_field(candidate, "source_refs")),
            )
        )
        out.append(
            new_item(
                kind=kind,
                status=status,
                scope=scope,
                scope_ref=scope_ref,
                title=title,
                why_now=_field(candidate, "why_now") or "Bounded local research interest.",
                priority=priority,
                confidence=confidence,
                source=source,
                source_ref=source_ref,
                evidence_refs=evidence_refs,
                run_refs=(),
                now=now,
                metadata={
                    "related_concepts": list(_field(candidate, "related_concepts") or ())[:6],
                    "shared_neighbors": list(_field(candidate, "shared_neighbors") or ())[:6],
                    "strong_support": strong,
                },
            )
        )
    return out


def items_from_work_checkpoint(
    store: Any,
    *,
    session_id: str,
    project: str,
    now: str,
) -> list[GhostWorkItem]:
    """Convert one work checkpoint into a follow-up item."""
    if store is None or not session_id:
        return []
    try:
        checkpoint = store.load(session_id)
    except Exception:
        return []
    if checkpoint is None:
        return []
    checkpoint_project = _common.normalize_project(getattr(checkpoint, "project", "") or project)
    project_ref = _project_ref(project)
    if project_ref and _project_ref(checkpoint_project) != project_ref:
        return []
    title = _clean_item_text(getattr(checkpoint, "original_task", ""), max_chars=MAX_WORK_TITLE_CHARS)
    if not title:
        return []
    status = str(getattr(checkpoint, "status", "") or "")
    if status not in {"interrupted", "ready_for_review", "fixing_review", "working"}:
        return []
    return [
        new_item(
            kind="project_followup",
            status="queued" if status in {"interrupted", "ready_for_review", "fixing_review"} else "candidate",
            scope="session",
            scope_ref=_session_ref(session_id),
            title=title,
            why_now=f"Local checkpoint is {clip_signal_text(status, 40)}.",
            priority=0.86,
            confidence=0.9,
            source="work_checkpoint",
            source_ref=clip_signal_text(getattr(checkpoint, "run_id", ""), MAX_WORK_REF_CHARS),
            evidence_refs=(f"checkpoint:{_session_ref(session_id)}",),
            run_refs=(clip_signal_text(getattr(checkpoint, "run_id", ""), MAX_WORK_REF_CHARS),),
            now=now,
            metadata={"checkpoint_status": status, "project_ref": _project_ref(checkpoint_project)},
        )
    ]


def items_from_run_projection(
    projection: Any,
    *,
    session_id: str,
    project: str,
    now: str,
) -> list[GhostWorkItem]:
    """Convert an unfinished run-ledger projection into a follow-up item."""
    if projection is None or not clip_signal_text(getattr(projection, "run_id", ""), 120):
        return []
    stop_reason = clip_signal_text(getattr(projection, "stop_reason", ""), 80)
    mode = clip_signal_text(getattr(projection, "mode", ""), 40)
    tool_errors = _int(getattr(projection, "tool_errors", 0))
    if stop_reason not in {"error", "no_progress", "stopped"} and tool_errors <= 0:
        return []
    project_ref = _project_ref(project or getattr(projection, "project", ""))
    if not project_ref:
        return []
    title = f"Resume {mode or 'project'} run after {stop_reason or 'tool errors'}"
    return [
        new_item(
            kind="project_followup",
            status="queued" if stop_reason in {"error", "no_progress", "stopped"} else "candidate",
            scope="project",
            scope_ref=_common.normalize_project(project or getattr(projection, "project", "")),
            title=title,
            why_now="A bounded run ledger projection recorded unfinished local work.",
            priority=0.72,
            confidence=0.75,
            source="run_ledger",
            source_ref=clip_signal_text(getattr(projection, "run_id", ""), MAX_WORK_REF_CHARS),
            evidence_refs=(f"ledger:{clip_signal_text(getattr(projection, 'run_id', ''), MAX_WORK_REF_CHARS)}",),
            run_refs=(clip_signal_text(getattr(projection, "run_id", ""), MAX_WORK_REF_CHARS),),
            now=now,
            metadata={"stop_reason": stop_reason, "tool_errors": tool_errors, "session_ref": _session_ref(session_id)},
        )
    ]


def items_from_terminal_event(
    event: Mapping[str, object] | None,
    *,
    session_id: str,
    run_id: str,
    project: str,
    now: str,
) -> list[GhostWorkItem]:
    """Convert a review terminal event requesting changes into a coding item."""
    if not isinstance(event, Mapping):
        return []
    if str(event.get("mode") or "") != "review":
        return []
    summary = str(event.get("summary") or "")
    if "requested changes" not in summary.casefold():
        return []
    project_ref = _project_ref(project)
    if not project_ref:
        return []
    return [
        new_item(
            kind="coding",
            status="queued",
            scope="project",
            scope_ref=_common.normalize_project(project),
            title="Address local review findings",
            why_now="Review-only mode found issues in the current diff.",
            priority=0.78,
            confidence=0.82,
            source="review",
            source_ref=clip_signal_text(run_id or event.get("run_id"), MAX_WORK_REF_CHARS),
            evidence_refs=(f"review:{clip_signal_text(run_id or event.get('run_id'), MAX_WORK_REF_CHARS)}",),
            run_refs=(clip_signal_text(run_id or event.get("run_id"), MAX_WORK_REF_CHARS),),
            now=now,
            metadata={"session_ref": _session_ref(session_id)},
        )
    ]


__all__ = [
    "items_from_continuity",
    "items_from_research_interest_candidates",
    "items_from_run_projection",
    "items_from_terminal_event",
    "items_from_work_checkpoint",
    "new_item",
]
