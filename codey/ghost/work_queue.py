"""Bounded local work-item queue for Ghost continuity.

The queue is a local state machine, not an autonomous runner. It can remember
audited follow-up work and claim one item when the user explicitly asks to
continue. Task entry runs the claimed item; Ghost post-turn policy updates the
queue from terminal task facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field, is_dataclass, replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping
import uuid

from codey.ghost.affinity import apply_affinity_work_boost
from codey.ghost.continuity import GhostContinuityStore
from codey.ghost.event_log import (
    GhostEventLog,
    compact_result_payload as _compact_payload,
    event_file_stats as _event_file_stats,
)
from codey.ghost.numbers import clamp_unit_float
from codey.ghost.schema import clip_signal_text, contains_sensitive_signal_text
from codey.storage.local_store import (
    DEFAULT_STATE_HOME,
    project_key,
    read_json,
    session_key,
    write_json_atomic,
)
from codey.storage.event_state import reset_event_backed_state
from codey.storage.file_lock import with_file_lock
from codey.policies.prompt_safety import is_prompt_visible_text_safe


WORK_QUEUE_SCHEMA_VERSION = 1
MAX_WORK_ITEMS = 200
MAX_WORK_EVENTS = 5_000
MAX_WORK_STATE_BYTES = 1024 * 1024
MAX_WORK_EVENTS_BYTES = 1024 * 1024
MAX_WORK_TITLE_CHARS = 140
MAX_WORK_WHY_CHARS = 240
MAX_WORK_REF_CHARS = 160
MAX_WORK_REFS = 10
MAX_WORK_WARNINGS = 20
MAX_WORK_RETRIES = 3
DEFAULT_WORK_CLAIM_LEASE_SECONDS = 24 * 60 * 60
_PROJECTION_KIND = "ghost_work_items_projection"
_WORK_EVENT_TYPES = frozenset(
    {
        "ghost_work_item_observed",
        "ghost_work_item_transitioned",
        "ghost_work_items_deleted",
        "ghost_work_snapshot",
    }
)
WORK_ITEM_TRANSITION_MATRIX = {
    "claim": {"queued": frozenset({"running"})},
    "complete": {"running": frozenset({"done"})},
    "block": {
        "candidate": frozenset({"blocked"}),
        "queued": frozenset({"blocked"}),
        "running": frozenset({"blocked"}),
    },
    "release": {"running": frozenset({"queued", "blocked"})},
    "release_stale": {"running": frozenset({"queued", "blocked"})},
    "reject": {
        "candidate": frozenset({"rejected"}),
        "queued": frozenset({"rejected"}),
        "blocked": frozenset({"rejected"}),
    },
    "queue": {
        "candidate": frozenset({"queued"}),
        "blocked": frozenset({"queued"}),
        "rejected": frozenset({"queued"}),
    },
}
WORK_ITEM_TRANSITION_ACTIONS = frozenset(WORK_ITEM_TRANSITION_MATRIX)
_WORK_PRECONDITION_KEYS = frozenset({"expected_status", "expected_started_run_id", "expected_retry_count"})
_WORK_DELETE_PAYLOAD_KEYS = frozenset({"reason", "item_ids", "expected_items", "scope", "project_ref", "session_ref"})
_WORK_EVENT_KEYS = {
    "ghost_work_item_observed": frozenset({"schema_version", "type", "event_id", "ts", "item"}),
    "ghost_work_item_transitioned": frozenset(
        {"schema_version", "type", "event_id", "ts", "action", "item_id", "precondition", "patch"}
    ),
    "ghost_work_items_deleted": frozenset({"schema_version", "type", "event_id", "ts", "payload"}),
    "ghost_work_snapshot": frozenset({"schema_version", "type", "event_id", "ts", "reason", "items"}),
}
_WORK_TRANSITION_PATCH_KEYS = {
    "claim": frozenset({"status", "started_run_id", "retry_count", "lease_expires_at", "blocked_reason", "updated_at"}),
    "complete": frozenset(
        {"status", "completed_run_id", "proof_refs", "lease_expires_at", "blocked_reason", "updated_at"}
    ),
    "release": frozenset({"status", "started_run_id", "lease_expires_at", "blocked_reason", "updated_at"}),
    "release_stale": frozenset({"status", "started_run_id", "lease_expires_at", "blocked_reason", "updated_at"}),
    "block": frozenset({"status", "blocked_reason", "lease_expires_at", "updated_at"}),
    "reject": frozenset(
        {
            "status",
            "started_run_id",
            "completed_run_id",
            "proof_refs",
            "lease_expires_at",
            "blocked_reason",
            "updated_at",
        }
    ),
    "queue": frozenset(
        {
            "status",
            "started_run_id",
            "completed_run_id",
            "proof_refs",
            "retry_count",
            "lease_expires_at",
            "blocked_reason",
            "updated_at",
        }
    ),
}

WORK_ITEM_KINDS = frozenset(
    {
        "research",
        "coding",
        "review",
        "memory_sleep",
        "open_question",
        "project_followup",
    }
)
WORK_ITEM_STATUSES = frozenset(
    {
        "candidate",
        "queued",
        "running",
        "blocked",
        "done",
        "rejected",
        "expired",
    }
)
WORK_ITEM_SOURCES = frozenset(
    {
        "continuity",
        "concept_open_question",
        "research_note",
        "research_interest",
        "run_ledger",
        "review",
        "work_checkpoint",
        "sleep",
        "user",
    }
)
ACTIVE_STATUSES = frozenset({"candidate", "queued", "running", "blocked"})
CLAIMABLE_STATUSES = frozenset({"queued"})
TERMINAL_STATUSES = frozenset({"done", "rejected", "expired"})
TASKRUNNER_KINDS = frozenset({"research", "open_question", "coding", "project_followup", "review"})
KIND_PRIORITY = {
    "project_followup": 0,
    "coding": 1,
    "review": 2,
    "research": 3,
    "open_question": 4,
    "memory_sleep": 5,
}
STATUS_PRIORITY = {
    "running": 0,
    "queued": 1,
    "blocked": 2,
    "candidate": 3,
    "done": 4,
    "rejected": 5,
    "expired": 6,
}
SCOPE_PRIORITY = {
    "session": 0,
    "project": 1,
    "user": 2,
}

_STRICT_CONTINUATION_CN = frozenset(
    {
        "继续",
        "继续吧",
        "继续处理",
        "继续待办",
        "继续刚才的待办",
        "继续上一个待办",
        "处理待办",
        "处理下一个待办",
        "下一个",
        "接着做",
    }
)
_STRICT_CONTINUATION_EN = frozenset(
    {
        "continue",
        "continue please",
        "next",
        "next item",
        "handle pending item",
        "continue pending item",
        "continue saved task",
        "resume saved task",
        "resume queued task",
    }
)


@dataclass(frozen=True)
class GhostWorkItem:
    id: str
    kind: str
    status: str
    scope: str
    scope_ref: str
    title: str
    why_now: str
    priority: float
    confidence: float
    source: str
    source_ref: str
    evidence_refs: tuple[str, ...] = ()
    run_refs: tuple[str, ...] = ()
    started_run_id: str = ""
    completed_run_id: str = ""
    proof_refs: tuple[str, ...] = ()
    blocked_reason: str = ""
    retry_count: int = 0
    lease_expires_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    expires_at: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "scope": self.scope,
            "scope_ref": self.scope_ref,
            "title": self.title,
            "why_now": self.why_now,
            "priority": self.priority,
            "confidence": self.confidence,
            "source": self.source,
            "source_ref": self.source_ref,
            "evidence_refs": list(self.evidence_refs),
            "run_refs": list(self.run_refs),
            "started_run_id": self.started_run_id,
            "completed_run_id": self.completed_run_id,
            "proof_refs": list(self.proof_refs),
            "blocked_reason": self.blocked_reason,
            "retry_count": self.retry_count,
            "lease_expires_at": self.lease_expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "metadata": _clean_metadata(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "GhostWorkItem | None":
        if not isinstance(payload, dict):
            return None
        kind = _clean_kind(payload.get("kind"))
        status = _clean_status(payload.get("status"))
        scope = _clean_scope(payload.get("scope"))
        source = _clean_source(payload.get("source"))
        if not kind or not status or not scope or not source:
            return None
        item_id = clip_signal_text(payload.get("id"), 120)
        title = _clean_item_text(payload.get("title"), max_chars=MAX_WORK_TITLE_CHARS)
        why_now = _clean_item_text(payload.get("why_now"), max_chars=MAX_WORK_WHY_CHARS)
        if not item_id or not title:
            return None

        started_run_id = clip_signal_text(payload.get("started_run_id"), 120)
        completed_run_id = clip_signal_text(payload.get("completed_run_id"), 120)
        proof_refs = _bounded_refs(payload.get("proof_refs"))
        blocked_reason = clip_signal_text(payload.get("blocked_reason"), 120)
        lease_expires_at = clip_signal_text(payload.get("lease_expires_at"), 80)

        if status == "done":
            if not completed_run_id or not proof_refs or lease_expires_at or blocked_reason:
                return None
        elif status in {"queued", "candidate", "rejected"}:
            if started_run_id or completed_run_id or proof_refs or lease_expires_at or blocked_reason:
                return None
        elif status == "running":
            if not started_run_id or completed_run_id or proof_refs or blocked_reason:
                return None
        elif status == "blocked":
            if not blocked_reason or lease_expires_at or completed_run_id or proof_refs:
                return None
        else:
            return None

        return cls(
            id=item_id,
            kind=kind,
            status=status,
            scope=scope,
            scope_ref=clip_signal_text(payload.get("scope_ref"), 240),
            title=title,
            why_now=why_now,
            priority=_unit_float(payload.get("priority")),
            confidence=_unit_float(payload.get("confidence")),
            source=source,
            source_ref=clip_signal_text(payload.get("source_ref"), MAX_WORK_REF_CHARS),
            evidence_refs=_bounded_refs(payload.get("evidence_refs")),
            run_refs=_bounded_refs(payload.get("run_refs")),
            started_run_id=started_run_id,
            completed_run_id=completed_run_id,
            proof_refs=proof_refs,
            blocked_reason=blocked_reason,
            retry_count=max(0, _int(payload.get("retry_count"))),
            lease_expires_at=lease_expires_at,
            created_at=clip_signal_text(payload.get("created_at"), 80),
            updated_at=clip_signal_text(payload.get("updated_at"), 80),
            expires_at=clip_signal_text(payload.get("expires_at"), 80),
            metadata=_clean_metadata(payload.get("metadata")),
        )


@dataclass(frozen=True)
class GhostWorkSyncResult:
    ok: bool
    skipped_reason: str = ""
    items_changed: int = 0
    total_items: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class GhostWorkClaimResult:
    ok: bool
    item: GhostWorkItem | None = None
    mode: str = ""
    task: str = ""
    skipped_reason: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _WorkMutation:
    result: object
    append_events: tuple[dict[str, object], ...] = ()
    replace_events: tuple[dict[str, object], ...] | None = None
    items: tuple[GhostWorkItem, ...] = ()
    write_projection: bool = True
    compact: bool = True


class GhostWorkQueueStore:
    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.directory = Path(state_home) / "ghost"
        self.projection_path = self.directory / "work_items.json"
        self.events_path = self.directory / "work_events.jsonl"
        self.last_warnings: tuple[str, ...] = ()
        self._events_read_blocked = False
        self._events_blocked_reason = ""

    def _event_log(self) -> GhostEventLog:
        return GhostEventLog(
            self.events_path,
            schema_version=WORK_QUEUE_SCHEMA_VERSION,
            max_bytes=MAX_WORK_EVENTS_BYTES,
            max_warnings=MAX_WORK_WARNINGS,
            source_name="work_events.jsonl",
            allowed_event_kinds=_WORK_EVENT_TYPES,
            bad_row_policy="block",
            event_validator=lambda event: _valid_work_event(event),
        )

    def sync_from_sources(
        self,
        *,
        continuity_store: GhostContinuityStore | None = None,
        work_checkpoint_store: Any = None,
        run_projection: Any = None,
        terminal_event: Mapping[str, object] | None = None,
        research_interest_candidates: Iterable[Any] = (),
        session_id: str = "",
        run_id: str = "",
        project: str = "",
    ) -> GhostWorkSyncResult:
        try:
            now = _now()
            candidates: list[GhostWorkItem] = []
            candidates.extend(
                _items_from_continuity(
                    continuity_store,
                    session_id=session_id,
                    project=project,
                    now=now,
                )
            )
            candidates.extend(
                _items_from_research_interest_candidates(
                    research_interest_candidates,
                    session_id=session_id,
                    project=project,
                    now=now,
                )
            )
            candidates.extend(
                _items_from_work_checkpoint(
                    work_checkpoint_store,
                    session_id=session_id,
                    project=project,
                    now=now,
                )
            )
            candidates.extend(
                _items_from_run_projection(
                    run_projection,
                    session_id=session_id,
                    project=project,
                    now=now,
                )
            )
            candidates.extend(
                _items_from_terminal_event(
                    terminal_event,
                    session_id=session_id,
                    run_id=run_id,
                    project=project,
                    now=now,
                )
            )

            def decide(events: list[dict[str, object]]) -> _WorkMutation:
                items = _bounded_items(_items_from_events(events))
                expired = tuple(item for item in items if _is_expired(item, now))
                append_events: list[dict[str, object]] = []
                if expired:
                    append_events.append(
                        _items_deleted_event(
                            reason="expired",
                            expected_items=expired,
                            ts=now,
                        )
                    )
                    expired_ids = {item.id for item in expired}
                    items = _bounded_items(item for item in items if item.id not in expired_ids)
                if not candidates:
                    if not append_events:
                        self.last_warnings = ()
                        return _WorkMutation(
                            GhostWorkSyncResult(True, skipped_reason="no_sources", total_items=len(items)),
                            items=tuple(items),
                            write_projection=False,
                            compact=False,
                        )
                    new_items = _bounded_items(_items_from_events((*events, *append_events)))
                    return _WorkMutation(
                        GhostWorkSyncResult(
                            True, skipped_reason="no_sources", items_changed=len(expired), total_items=len(new_items)
                        ),
                        append_events=tuple(append_events),
                        items=tuple(new_items),
                    )
                observed_count = 0
                current = items
                for candidate in candidates:
                    before = _item_payloads(current)
                    current, changed = _merge_items(current, (candidate,), now=now)
                    current = _bounded_items(item for item in current if not _is_expired(item, now))
                    if changed and _item_payloads(current) != before:
                        append_events.append(_observed_event(candidate, ts=now))
                        observed_count += 1
                if not append_events:
                    self.last_warnings = ()
                    return _WorkMutation(
                        GhostWorkSyncResult(True, items_changed=0, total_items=len(current)),
                        items=tuple(current),
                        write_projection=False,
                        compact=False,
                    )
                new_items = _bounded_items(_items_from_events((*events, *append_events)))
                return _WorkMutation(
                    GhostWorkSyncResult(
                        True, items_changed=observed_count, total_items=len(new_items), warnings=self.last_warnings
                    ),
                    append_events=tuple(append_events),
                    items=tuple(new_items),
                )

            result = self._mutate_event_log(decide)
            if isinstance(result, GhostWorkSyncResult):
                return result
            return self._sync_failed("work_queue_error")
        except (OSError, TypeError, ValueError):
            if self._events_read_blocked:
                return self._sync_failed(self._events_blocked_reason or "events_read_blocked")
            if "work_event_write_failed" in self.last_warnings:
                return self._sync_failed("event_write_failed")
            return self._sync_failed("work_queue_error")

    def list_items(
        self,
        *,
        status: str = "",
        kind: str = "",
        scope: str = "",
        project: str = "",
        session_id: str = "",
    ) -> tuple[GhostWorkItem, ...]:
        with with_file_lock(self.events_path):
            return self._list_items_unlocked(
                status=status,
                kind=kind,
                scope=scope,
                project=project,
                session_id=session_id,
            )

    def _list_items_unlocked(
        self,
        *,
        status: str = "",
        kind: str = "",
        scope: str = "",
        project: str = "",
        session_id: str = "",
    ) -> tuple[GhostWorkItem, ...]:
        statuses = _filter_values(status, WORK_ITEM_STATUSES)
        kinds = _filter_values(kind, WORK_ITEM_KINDS)
        project_ref = _project_ref(project)
        session_ref = _session_ref(session_id)
        rows = []
        now = _now()
        for item in self._load_items_unlocked():
            if _is_expired(item, now):
                continue
            if statuses and item.status not in statuses:
                continue
            if kinds and item.kind not in kinds:
                continue
            if not _scope_filter_matches(item, scope=scope, project_ref=project_ref, session_ref=session_ref):
                continue
            rows.append(item)
        return tuple(sorted(rows, key=_item_sort_key))

    def claim_next(
        self,
        *,
        session_id: str = "",
        project: str = "",
        run_id: str,
        user_request: str = "",
        lease_seconds: int = DEFAULT_WORK_CLAIM_LEASE_SECONDS,
        affinity_hints: Iterable[Any] = (),
    ) -> GhostWorkClaimResult:
        if not clip_signal_text(run_id, 120):
            return GhostWorkClaimResult(False, skipped_reason="run_id_required")
        if not is_strict_work_continuation(user_request):
            return GhostWorkClaimResult(False, skipped_reason="not_continuation")
        try:

            def decide(events: list[dict[str, object]]) -> _WorkMutation:
                now = _now()
                append_events: list[dict[str, object]] = []
                items = _bounded_items(_items_from_events(events))
                for item in items:
                    if not _is_stale_claim(item, now):
                        continue
                    next_status = "blocked" if item.retry_count >= MAX_WORK_RETRIES else "queued"
                    append_events.append(
                        _transition_event(
                            item,
                            action="release_stale",
                            patch={
                                "status": next_status,
                                "started_run_id": "" if next_status == "queued" else item.started_run_id,
                                "lease_expires_at": "",
                                "blocked_reason": "stale_claim" if next_status == "blocked" else "",
                                "updated_at": now,
                            },
                            ts=now,
                        )
                    )
                if append_events:
                    items = _bounded_items(_items_from_events((*events, *append_events)))
                candidate = _next_claimable_item(
                    items,
                    session_id=session_id,
                    project=project,
                    affinity_hints=affinity_hints,
                )
                if candidate is None:
                    result = GhostWorkClaimResult(
                        False,
                        skipped_reason="no_queued_item",
                        warnings=self.last_warnings,
                    )
                    if not append_events:
                        return _WorkMutation(result, items=tuple(items), write_projection=False, compact=False)
                    new_items = _bounded_items(_items_from_events((*events, *append_events)))
                    return _WorkMutation(result, append_events=tuple(append_events), items=tuple(new_items))
                mode = mode_for_work_item(replace(candidate, status="running"), project=project)
                if not mode:
                    return _WorkMutation(
                        GhostWorkClaimResult(False, skipped_reason="unrunnable_item"),
                        items=tuple(items),
                        write_projection=False,
                        compact=False,
                    )
                claimed = replace(
                    candidate,
                    status="running",
                    started_run_id=clip_signal_text(run_id, 120),
                    retry_count=candidate.retry_count + 1,
                    lease_expires_at=_future_ts(now, lease_seconds),
                    completed_run_id="",
                    proof_refs=(),
                    blocked_reason="",
                    updated_at=now,
                )
                append_events.append(
                    _transition_event(
                        candidate,
                        action="claim",
                        patch={
                            "status": "running",
                            "started_run_id": claimed.started_run_id,
                            "retry_count": claimed.retry_count,
                            "lease_expires_at": claimed.lease_expires_at,
                            "updated_at": now,
                            "blocked_reason": "",
                        },
                        ts=now,
                    )
                )
                new_items = _bounded_items(_items_from_events((*events, *append_events)))
                return _WorkMutation(
                    GhostWorkClaimResult(
                        True,
                        item=claimed,
                        mode=mode,
                        task=render_work_item_task(claimed, user_request=user_request),
                        warnings=self.last_warnings,
                    ),
                    append_events=tuple(append_events),
                    items=tuple(new_items),
                )

            result = self._mutate_event_log(decide)
            if isinstance(result, GhostWorkClaimResult):
                return result
            return GhostWorkClaimResult(False, skipped_reason="work_queue_error")
        except (OSError, TypeError, ValueError):
            if self._events_read_blocked:
                return GhostWorkClaimResult(
                    False,
                    skipped_reason=self._events_blocked_reason or "events_read_blocked",
                    warnings=self.last_warnings,
                )
            if "work_event_write_failed" in self.last_warnings:
                return GhostWorkClaimResult(False, skipped_reason="event_write_failed", warnings=self.last_warnings)
            return GhostWorkClaimResult(False, skipped_reason="work_queue_error")

    def complete_item(
        self,
        item_id: str,
        *,
        run_id: str,
        proof_refs: Iterable[object],
    ) -> GhostWorkItem | None:
        expected_run_id = clip_signal_text(run_id, 120)
        if not expected_run_id:
            return None
        refs = _bounded_refs(tuple(proof_refs))
        try:

            def decide(events: list[dict[str, object]]) -> _WorkMutation:
                items = _bounded_items(_items_from_events(events))
                current = _find_item(items, item_id)
                if current is None or current.status != "running":
                    return _WorkMutation(None, items=tuple(items), write_projection=False, compact=False)
                if not current.started_run_id or current.started_run_id != expected_run_id:
                    return _WorkMutation(None, items=tuple(items), write_projection=False, compact=False)
                now = _now()
                if not refs or not _primary_proof_matches_item_kind(current, refs):
                    blocked = replace(
                        current,
                        status="blocked",
                        blocked_reason="missing_proof",
                        completed_run_id="",
                        proof_refs=(),
                        lease_expires_at="",
                        updated_at=now,
                    )
                    event = _transition_event(
                        current,
                        action="block",
                        patch={
                            "status": "blocked",
                            "blocked_reason": "missing_proof",
                            "lease_expires_at": "",
                            "updated_at": now,
                        },
                        ts=now,
                    )
                    new_items = _bounded_items(_items_from_events((*events, event)))
                    return _WorkMutation(blocked, append_events=(event,), items=tuple(new_items))
                completed = replace(
                    current,
                    status="done",
                    completed_run_id=expected_run_id,
                    proof_refs=refs,
                    blocked_reason="",
                    lease_expires_at="",
                    updated_at=now,
                )
                event = _transition_event(
                    current,
                    action="complete",
                    patch={
                        "status": "done",
                        "completed_run_id": expected_run_id,
                        "proof_refs": refs,
                        "blocked_reason": "",
                        "lease_expires_at": "",
                        "updated_at": now,
                    },
                    ts=now,
                )
                new_items = _bounded_items(_items_from_events((*events, event)))
                return _WorkMutation(completed, append_events=(event,), items=tuple(new_items))

            result = self._mutate_event_log(decide)
            return result if isinstance(result, GhostWorkItem) else None
        except (OSError, TypeError, ValueError):
            if self._events_read_blocked:
                raise OSError("ghost work events are unreadable")
            raise

    def block_item(
        self,
        item_id: str,
        *,
        run_id: str = "",
        blocked_reason: str = "",
    ) -> GhostWorkItem | None:
        return self._transition_item(
            item_id,
            expected_run_id=run_id,
            status="blocked",
            blocked_reason=clip_signal_text(blocked_reason or "blocked", 120),
            action="block",
        )

    def release_item(
        self,
        item_id: str,
        *,
        run_id: str = "",
        reason: str = "",
    ) -> GhostWorkItem | None:
        expected_run_id = clip_signal_text(run_id, 120)
        try:

            def decide(events: list[dict[str, object]]) -> _WorkMutation:
                items = _bounded_items(_items_from_events(events))
                current = _find_item(items, item_id)
                if current is None or current.status != "running":
                    return _WorkMutation(None, items=tuple(items), write_projection=False, compact=False)
                if expected_run_id and current.started_run_id and current.started_run_id != expected_run_id:
                    return _WorkMutation(None, items=tuple(items), write_projection=False, compact=False)
                now = _now()
                next_status = "blocked" if current.retry_count >= MAX_WORK_RETRIES else "queued"
                blocked_reason = clip_signal_text(reason or "retry_limit" if next_status == "blocked" else "", 120)
                updated = replace(
                    current,
                    status=next_status,
                    started_run_id="" if next_status == "queued" else current.started_run_id,
                    completed_run_id="",
                    proof_refs=(),
                    lease_expires_at="",
                    blocked_reason=blocked_reason,
                    updated_at=now,
                )
                event = _transition_event(
                    current,
                    action="release",
                    patch={
                        "status": next_status,
                        "started_run_id": updated.started_run_id,
                        "lease_expires_at": "",
                        "blocked_reason": blocked_reason,
                        "updated_at": now,
                    },
                    ts=now,
                )
                new_items = _bounded_items(_items_from_events((*events, event)))
                return _WorkMutation(updated, append_events=(event,), items=tuple(new_items))

            result = self._mutate_event_log(decide)
            return result if isinstance(result, GhostWorkItem) else None
        except (OSError, TypeError, ValueError):
            if self._events_read_blocked:
                raise OSError("ghost work events are unreadable")
            raise

    def reject_item(self, item_id: str) -> GhostWorkItem | None:
        return self._transition_item(item_id, status="rejected", blocked_reason="", action="reject")

    def queue_item(self, item_id: str) -> GhostWorkItem | None:
        try:

            def decide(events: list[dict[str, object]]) -> _WorkMutation:
                items = _bounded_items(_items_from_events(events))
                current = _find_item(items, item_id)
                if current is None:
                    return _WorkMutation(None, items=tuple(items), write_projection=False, compact=False)
                if current.status == "queued":
                    return _WorkMutation(current, items=tuple(items), write_projection=False, compact=False)
                if current.status not in {"candidate", "blocked", "rejected"}:
                    return _WorkMutation(None, items=tuple(items), write_projection=False, compact=False)
                now = _now()
                queued = replace(
                    current,
                    status="queued",
                    retry_count=0,
                    started_run_id="",
                    completed_run_id="",
                    proof_refs=(),
                    blocked_reason="",
                    lease_expires_at="",
                    updated_at=now,
                )
                event = _transition_event(
                    current,
                    action="queue",
                    patch={
                        "status": "queued",
                        "retry_count": 0,
                        "started_run_id": "",
                        "completed_run_id": "",
                        "proof_refs": (),
                        "blocked_reason": "",
                        "lease_expires_at": "",
                        "updated_at": now,
                    },
                    ts=now,
                )
                new_items = _bounded_items(_items_from_events((*events, event)))
                return _WorkMutation(queued, append_events=(event,), items=tuple(new_items))

            result = self._mutate_event_log(decide)
            return result if isinstance(result, GhostWorkItem) else None
        except (OSError, TypeError, ValueError):
            if self._events_read_blocked:
                raise OSError("ghost work events are unreadable")
            raise

    def reconcile_stale_claims(self) -> GhostWorkSyncResult:
        try:

            def decide(events: list[dict[str, object]]) -> _WorkMutation:
                now = _now()
                items = _bounded_items(_items_from_events(events))
                append_events: list[dict[str, object]] = []
                for item in items:
                    if not _is_stale_claim(item, now):
                        continue
                    next_status = "blocked" if item.retry_count >= MAX_WORK_RETRIES else "queued"
                    append_events.append(
                        _transition_event(
                            item,
                            action="release_stale",
                            patch={
                                "status": next_status,
                                "started_run_id": "" if next_status == "queued" else item.started_run_id,
                                "lease_expires_at": "",
                                "blocked_reason": "stale_claim" if next_status == "blocked" else "",
                                "updated_at": now,
                            },
                            ts=now,
                        )
                    )
                if not append_events:
                    self.last_warnings = ()
                    return _WorkMutation(
                        GhostWorkSyncResult(True, skipped_reason="no_stale_claims", total_items=len(items)),
                        items=tuple(items),
                        write_projection=False,
                        compact=False,
                    )
                new_items = _bounded_items(_items_from_events((*events, *append_events)))
                return _WorkMutation(
                    GhostWorkSyncResult(
                        True, items_changed=len(append_events), total_items=len(new_items), warnings=self.last_warnings
                    ),
                    append_events=tuple(append_events),
                    items=tuple(new_items),
                )

            result = self._mutate_event_log(decide)
            if isinstance(result, GhostWorkSyncResult):
                return result
            return self._sync_failed("work_queue_error")
        except (OSError, TypeError, ValueError):
            if self._events_read_blocked:
                return self._sync_failed(self._events_blocked_reason or "events_read_blocked")
            if "work_event_write_failed" in self.last_warnings:
                return self._sync_failed("event_write_failed")
            return self._sync_failed("work_queue_error")

    def export_state(self) -> dict[str, object]:
        with with_file_lock(self.events_path):
            events_missing = not self.events_path.is_file()
            events = self._read_events_unlocked()
            event_warnings = self.last_warnings
            if self._events_read_blocked:
                items = self._load_projection_items_unlocked()
            elif events_missing:
                items = self._load_projection_items_unlocked()
                if items:
                    event_warnings = ("work_events_missing",)
            else:
                items = tuple(_items_from_events(events))
            projection = _projection_payload(items, generated_at=_now(), warnings=event_warnings)
            return {
                "schema_version": WORK_QUEUE_SCHEMA_VERSION,
                "work_queue": projection,
                "work_events": events,
                "warnings": list(event_warnings),
            }

    def reset_all(self) -> bool:
        try:
            reset_event_backed_state(self.events_path, self.projection_path)
            self.last_warnings = ()
            self._events_read_blocked = False
            self._events_blocked_reason = ""
            return True
        except OSError:
            return False

    def delete_scope(
        self,
        scope: str,
        *,
        project: str = "",
        session_id: str = "",
    ) -> dict[str, object]:
        normalized_scope = _clean_scope(scope)
        if not normalized_scope:
            raise ValueError("scope must be user, project, or session")
        project_ref = _project_ref(project)
        session_ref = _session_ref(session_id)
        if normalized_scope == "project" and not project_ref:
            raise ValueError("project is required for project scope deletion")
        if normalized_scope == "session" and not session_ref:
            raise ValueError("session_id is required for session scope deletion")
        try:

            def decide(events: list[dict[str, object]]) -> _WorkMutation:
                items = _bounded_items(_items_from_events(events))
                removed = [
                    item
                    for item in items
                    if _scope_filter_matches(
                        item,
                        scope=normalized_scope,
                        project_ref=project_ref,
                        session_ref=session_ref,
                    )
                ]
                if not removed:
                    return _WorkMutation(
                        {"removed": 0, "warnings": []}, items=tuple(items), write_projection=False, compact=False
                    )
                event = _items_deleted_event(
                    reason="scope_deleted",
                    scope=normalized_scope,
                    project_ref=project_ref if normalized_scope == "project" else "",
                    session_ref=session_ref if normalized_scope == "session" else "",
                    ts=_now(),
                )
                new_items = _bounded_items(_items_from_events((*events, event)))
                return _WorkMutation(
                    {"removed": len(removed), "warnings": []}, append_events=(event,), items=tuple(new_items)
                )

            result = self._mutate_event_log(decide)
            return result if isinstance(result, dict) else {"removed": 0, "warnings": ["work_queue_error"]}
        except (OSError, TypeError, ValueError):
            if self._events_read_blocked:
                raise OSError("ghost work events are unreadable")
            raise

    def rebuild_from_events(self) -> bool:
        try:
            with with_file_lock(self.events_path):
                events = self._events_for_mutation_locked()
                self._write_projection(_bounded_items(_items_from_events(events)), warnings=[])
            return True
        except (OSError, TypeError, ValueError):
            return False

    def compact_if_needed(self) -> dict[str, object]:
        before = _event_file_stats(
            self.events_path,
            max_bytes=MAX_WORK_EVENTS_BYTES,
            too_large_warning="work_events_too_large",
            unreadable_warning="work_events_unreadable",
        )
        try:
            with with_file_lock(self.events_path):
                before = _event_file_stats(
                    self.events_path,
                    max_bytes=MAX_WORK_EVENTS_BYTES,
                    too_large_warning="work_events_too_large",
                    unreadable_warning="work_events_unreadable",
                )
                if not self.events_path.exists() and self.projection_path.exists():
                    warning = "work_events_missing"
                    self.last_warnings = (warning,)
                    return _compact_payload(False, False, before, before, (warning,), warning_cleaner=_bounded_warnings)
                if not before["readable"]:
                    warning = str(before["warning"] or "work_events_unreadable")
                    self.last_warnings = (warning,)
                    return _compact_payload(False, False, before, before, (warning,), warning_cleaner=_bounded_warnings)
                if before["events"] <= MAX_WORK_EVENTS and before["bytes"] <= MAX_WORK_EVENTS_BYTES:
                    return _compact_payload(
                        True, False, before, before, self.last_warnings, warning_cleaner=_bounded_warnings
                    )
                events = self._events_for_mutation_locked()
                items = _bounded_items(_items_from_events(events))
                self._write_events_atomic([_snapshot_event(items, ts=_now(), reason="events_compacted")])
                self._write_projection(items, warnings=[])
                after = _event_file_stats(
                    self.events_path,
                    max_bytes=MAX_WORK_EVENTS_BYTES,
                    too_large_warning="work_events_too_large",
                    unreadable_warning="work_events_unreadable",
                )
                return _compact_payload(
                    True, after != before, before, after, self.last_warnings, warning_cleaner=_bounded_warnings
                )
        except (OSError, TypeError, ValueError):
            warning = "events_read_blocked" if self._events_read_blocked else "work_compaction_failed"
            self.last_warnings = _bounded_warnings((*self.last_warnings, warning))
            return _compact_payload(
                False, False, before, before, self.last_warnings, warning_cleaner=_bounded_warnings
            )

    def _transition_item(
        self,
        item_id: str,
        *,
        status: str,
        expected_run_id: str = "",
        blocked_reason: str = "",
        action: str,
    ) -> GhostWorkItem | None:
        target_status = _clean_status(status)
        expected = clip_signal_text(expected_run_id, 120)
        if not target_status:
            return None
        try:

            def decide(events: list[dict[str, object]]) -> _WorkMutation:
                items = _bounded_items(_items_from_events(events))
                current = _find_item(items, item_id)
                if current is None:
                    return _WorkMutation(None, items=tuple(items), write_projection=False, compact=False)
                if not _transition_allowed(current, target_status, action=action):
                    return _WorkMutation(None, items=tuple(items), write_projection=False, compact=False)
                if expected and current.started_run_id and current.started_run_id != expected:
                    return _WorkMutation(None, items=tuple(items), write_projection=False, compact=False)
                now = _now()
                patch: dict[str, object] = {
                    "status": target_status,
                    "updated_at": now,
                }
                if target_status == "blocked":
                    reason = clip_signal_text(blocked_reason or "blocked", 120)
                    patch["blocked_reason"] = reason
                    patch["lease_expires_at"] = ""
                    updated = replace(
                        current,
                        status="blocked",
                        blocked_reason=reason,
                        completed_run_id="",
                        proof_refs=(),
                        lease_expires_at="",
                        updated_at=now,
                    )
                elif target_status == "rejected":
                    patch["lease_expires_at"] = ""
                    patch["blocked_reason"] = ""
                    patch["started_run_id"] = ""
                    patch["completed_run_id"] = ""
                    patch["proof_refs"] = ()
                    updated = replace(
                        current,
                        status="rejected",
                        started_run_id="",
                        completed_run_id="",
                        proof_refs=(),
                        blocked_reason="",
                        lease_expires_at="",
                        updated_at=now,
                    )
                else:
                    return _WorkMutation(None, items=tuple(items), write_projection=False, compact=False)

                event = _transition_event(
                    current,
                    action=action,
                    patch=patch,
                    ts=now,
                )
                new_items = _bounded_items(_items_from_events((*events, event)))
                return _WorkMutation(updated, append_events=(event,), items=tuple(new_items))

            result = self._mutate_event_log(decide)
            return result if isinstance(result, GhostWorkItem) else None
        except (OSError, TypeError, ValueError):
            if self._events_read_blocked:
                raise OSError("ghost work events are unreadable")
            raise

    def _sync_failed(self, reason: str) -> GhostWorkSyncResult:
        warnings = self.last_warnings or ((reason,) if reason else ())
        self.last_warnings = _bounded_warnings(warnings)
        return GhostWorkSyncResult(False, skipped_reason=reason, warnings=self.last_warnings)

    def _load_items_unlocked(self) -> tuple[GhostWorkItem, ...]:
        if self.events_path.exists():
            events = self._read_events_unlocked()
            if not self._events_read_blocked:
                return tuple(_bounded_items(_items_from_events(events)))
            return self._load_projection_items_unlocked()
        projection = self._load_projection_items_unlocked()
        if projection:
            self.last_warnings = ("work_events_missing",)
        return projection

    def _load_projection_items_unlocked(self) -> tuple[GhostWorkItem, ...]:
        payload = read_json(self.projection_path, max_bytes=MAX_WORK_STATE_BYTES)
        if not isinstance(payload, dict):
            return ()
        if payload.get("schema_version") != WORK_QUEUE_SCHEMA_VERSION:
            return ()
        if payload.get("kind") != _PROJECTION_KIND:
            return ()
        return tuple(
            _bounded_items(
                item
                for item in (GhostWorkItem.from_payload(row) for row in _list(payload.get("items")))
                if item is not None
            )
        )

    def _read_events_unlocked(self) -> list[dict[str, object]]:
        self._events_read_blocked = False
        self._events_blocked_reason = ""
        read = self._event_log().read_locked()
        if read.blocked:
            self._events_read_blocked = True
            self.last_warnings = _event_read_warnings(read.warnings)
            self._events_blocked_reason = "events_read_blocked"
            return []

        rows = list(read.rows)
        warnings = list(_event_read_warnings(read.warnings))
        by_id: dict[str, GhostWorkItem] = {}
        for index, event in enumerate(rows, start=1):
            event_type = str(event.get("type") or "")
            now = _event_ts(event)
            if event_type == "ghost_work_snapshot":
                by_id = {item.id: item for item in _snapshot_items(event)}
            elif event_type == "ghost_work_item_observed":
                item = GhostWorkItem.from_payload(event.get("item"))
                if item is None:
                    warnings.append(f"work_events.jsonl:{index}:invalid_event")
                    self._events_read_blocked = True
                    break
                item = replace(item, created_at=item.created_at or now, updated_at=item.updated_at or now)
                merged, _changed = _merge_items(by_id.values(), (item,), now=now)
                by_id = {row.id: row for row in _bounded_items(merged)}
            elif event_type == "ghost_work_item_transitioned":
                res = _apply_transition_event(by_id, event, now=now)
                if res == "invalid":
                    warnings.append(f"work_events.jsonl:{index}:invalid_event")
                    self._events_read_blocked = True
                    break
            elif event_type == "ghost_work_items_deleted":
                _apply_items_deleted_event(by_id, event)

        self.last_warnings = _bounded_warnings(warnings)
        if self._events_read_blocked:
            self._events_blocked_reason = "events_read_blocked"
            return []
        return rows

    def _events_for_mutation_locked(self) -> list[dict[str, object]]:
        if not self.events_path.exists():
            if self.projection_path.exists():
                self._events_read_blocked = True
                self._events_blocked_reason = "work_events_missing"
                self.last_warnings = ("work_events_missing",)
                raise OSError("ghost work events are missing")
            self._events_read_blocked = False
            self._events_blocked_reason = ""
            self.last_warnings = ()
            return []
        events = self._read_events_unlocked()
        if self._events_read_blocked:
            raise OSError("ghost work events are unreadable")
        return events

    def _mutate_event_log(self, decide: Any) -> object:
        with with_file_lock(self.events_path):
            events = self._events_for_mutation_locked()
            mutation = decide(events)
            if not isinstance(mutation, _WorkMutation):
                raise TypeError("invalid work mutation result")
            try:
                if mutation.replace_events is not None:
                    self._write_events_atomic(mutation.replace_events)
                elif mutation.append_events:
                    self._write_events_atomic((*events, *mutation.append_events))
            except (OSError, TypeError, ValueError):
                self.last_warnings = _bounded_warnings((*self.last_warnings, "work_event_write_failed"))
                raise
            if mutation.write_projection:
                self._write_projection_best_effort(mutation.items)
            if mutation.compact:
                self._compact_if_needed_locked(mutation.items)
            result = mutation.result
            if is_dataclass(result) and not isinstance(result, type) and hasattr(result, "warnings"):
                return replace(result, warnings=self.last_warnings)
            if isinstance(result, dict):
                return dict(result, warnings=list(self.last_warnings))
            return result

    def _write_projection(self, items: Iterable[GhostWorkItem], *, warnings: Iterable[str]) -> None:
        write_json_atomic(
            self.projection_path,
            _projection_payload(items, generated_at=_now(), warnings=warnings),
            max_bytes=MAX_WORK_STATE_BYTES,
        )

    def _write_projection_best_effort(self, items: Iterable[GhostWorkItem]) -> None:
        try:
            self._write_projection(items, warnings=[])
        except (OSError, TypeError, ValueError):
            self.last_warnings = _bounded_warnings((*self.last_warnings, "work_projection_write_failed"))

    def _write_events_atomic(self, events: Iterable[dict[str, object]]) -> None:
        self._event_log().write_atomic_locked(events)

    def _compact_if_needed_locked(self, items: Iterable[GhostWorkItem]) -> None:
        stats = _event_file_stats(
            self.events_path,
            max_bytes=MAX_WORK_EVENTS_BYTES,
            too_large_warning="work_events_too_large",
            unreadable_warning="work_events_unreadable",
        )
        if not stats["readable"]:
            self.last_warnings = (str(stats["warning"] or "work_events_unreadable"),)
            return
        if stats["events"] <= MAX_WORK_EVENTS and stats["bytes"] <= MAX_WORK_EVENTS_BYTES:
            return
        try:
            self._write_events_atomic([_snapshot_event(items, ts=_now(), reason="events_compacted")])
        except (OSError, TypeError, ValueError):
            self.last_warnings = _bounded_warnings((*self.last_warnings, "work_compaction_failed"))


def is_strict_work_continuation(value: object) -> bool:
    normalized = _normalize_continuation_text(value)
    if not normalized:
        return False
    if normalized in _STRICT_CONTINUATION_CN or normalized in _STRICT_CONTINUATION_EN:
        return True
    return False


def mode_for_work_item(item: GhostWorkItem | None, *, project: str = "") -> str:
    if item is None or item.status != "running":
        return ""
    if item.kind in {"research", "open_question"}:
        return "research"
    if item.kind in {"coding", "project_followup"}:
        return "project" if str(project or "").strip() else ""
    if item.kind == "review":
        return "review" if str(project or "").strip() else ""
    return ""


def render_work_item_task(item: GhostWorkItem, *, user_request: str = "") -> str:
    title = _clean_item_text(item.title, max_chars=MAX_WORK_TITLE_CHARS)
    why = _clean_item_text(item.why_now, max_chars=MAX_WORK_WHY_CHARS)
    current = _clean_item_text(user_request, max_chars=80)
    lines = [
        "Continue this saved local task.",
        f"Task: {title}.",
    ]
    if why:
        lines.append(f"Reason: {why}.")
    if current:
        lines.append(f"Current request: {current}.")
    lines.append("The current user request overrides this saved task if they conflict.")
    return "\n".join(lines)


def proof_refs_from_task_event(
    item: GhostWorkItem | None,
    event: Mapping[str, object] | None,
    *,
    run_projection: Any = None,
) -> tuple[str, ...]:
    if item is None or not isinstance(event, Mapping):
        return ()
    run_id = clip_signal_text(event.get("run_id") or getattr(run_projection, "run_id", ""), 120)
    proof_run_id = _proof_run_ref(run_id)
    refs: list[str] = []
    primary_refs: list[str] = []
    if proof_run_id:
        refs.append(f"ledger:{proof_run_id}")
    receipt = event.get("receipt")
    if isinstance(receipt, Mapping) and receipt and proof_run_id:
        refs.append(f"receipt:{proof_run_id}")
        if _receipt_proves_project_work(receipt):
            primary_refs.append(f"receipt:{proof_run_id}")
    changes = event.get("changes")
    if isinstance(changes, Mapping) and _int(changes.get("changed_count")) > 0:
        primary_refs.append(f"diff:{proof_run_id}")
    research = event.get("research")
    if isinstance(research, Mapping):
        synthesis = clip_signal_text(research.get("synthesis_id"), MAX_WORK_REF_CHARS)
        if synthesis:
            primary_refs.append(f"research:{synthesis}")
        elif research.get("citation_map") or research.get("evidence_items"):
            primary_refs.append(f"research:{proof_run_id}")
    if str(event.get("mode") or "") == "review":
        primary_refs.append(f"review:{proof_run_id}")
    if getattr(run_projection, "complete", False):
        refs.append(f"projection:{proof_run_id}")
    primary_refs = list(_bounded_refs(primary_refs))
    if not _primary_proof_matches_item_kind(item, primary_refs):
        return ()
    return _bounded_refs((*refs, *primary_refs))


def _items_from_continuity(
    store: GhostContinuityStore | None,
    *,
    session_id: str,
    project: str,
    now: str,
) -> list[GhostWorkItem]:
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
            scope_ref = _normalize_project(scope_ref or project)
        else:
            scope_ref = ""
        out.append(
            _new_item(
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


def _items_from_research_interest_candidates(
    candidates: Iterable[Any],
    *,
    session_id: str,
    project: str,
    now: str,
) -> list[GhostWorkItem]:
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
            scope_ref = _normalize_project(raw_scope_ref or project)
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
            _new_item(
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


def _items_from_work_checkpoint(
    store: Any,
    *,
    session_id: str,
    project: str,
    now: str,
) -> list[GhostWorkItem]:
    if store is None or not session_id:
        return []
    try:
        checkpoint = store.load(session_id)
    except Exception:
        return []
    if checkpoint is None:
        return []
    checkpoint_project = _normalize_project(getattr(checkpoint, "project", "") or project)
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
        _new_item(
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


def _items_from_run_projection(
    projection: Any,
    *,
    session_id: str,
    project: str,
    now: str,
) -> list[GhostWorkItem]:
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
        _new_item(
            kind="project_followup",
            status="queued" if stop_reason in {"error", "no_progress", "stopped"} else "candidate",
            scope="project",
            scope_ref=_normalize_project(project or getattr(projection, "project", "")),
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


def _items_from_terminal_event(
    event: Mapping[str, object] | None,
    *,
    session_id: str,
    run_id: str,
    project: str,
    now: str,
) -> list[GhostWorkItem]:
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
        _new_item(
            kind="coding",
            status="queued",
            scope="project",
            scope_ref=_normalize_project(project),
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


def _new_item(
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


def _merge_items(
    existing: Iterable[GhostWorkItem],
    incoming: Iterable[GhostWorkItem],
    *,
    now: str,
) -> tuple[list[GhostWorkItem], list[GhostWorkItem]]:
    by_id = {item.id: item for item in existing}
    changed: list[GhostWorkItem] = []
    for item in incoming:
        if not item.title:
            continue
        current = by_id.get(item.id)
        if current is None:
            by_id[item.id] = item
            changed.append(item)
            continue
        if current.status in TERMINAL_STATUSES:
            continue
        status = current.status
        if current.status == "candidate" and item.status == "queued":
            status = "queued"
        merged = replace(
            current,
            status=status,
            title=item.title,
            why_now=item.why_now or current.why_now,
            priority=max(current.priority, item.priority),
            confidence=max(current.confidence, item.confidence),
            evidence_refs=_bounded_refs((*current.evidence_refs, *item.evidence_refs)),
            run_refs=_bounded_refs((*current.run_refs, *item.run_refs)),
            updated_at=now,
            metadata={**dict(current.metadata), **dict(item.metadata)},
        )
        if _meaningful_item_payload(merged) == _meaningful_item_payload(current):
            continue
        by_id[item.id] = merged
        changed.append(merged)
    return list(by_id.values()), changed


def _next_claimable_item(
    items: Iterable[GhostWorkItem],
    *,
    session_id: str,
    project: str,
    affinity_hints: Iterable[Any] = (),
) -> GhostWorkItem | None:
    project_ref = _project_ref(project)
    session_ref = _session_ref(session_id)
    candidates = [
        item
        for item in items
        if item.status in CLAIMABLE_STATUSES
        and item.kind in TASKRUNNER_KINDS
        and item.retry_count < MAX_WORK_RETRIES
        and _scope_matches(item, project_ref=project_ref, session_ref=session_ref)
        and mode_for_work_item(replace(item, status="running"), project=project)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: _claim_sort_key(item, affinity_hints))[0]


def _find_item(items: Iterable[GhostWorkItem], item_id: str) -> GhostWorkItem | None:
    target = clip_signal_text(item_id, 120)
    for item in items:
        if item.id == target:
            return item
    return None


def _bounded_items(items: Iterable[GhostWorkItem]) -> list[GhostWorkItem]:
    rows = [item for item in items if isinstance(item, GhostWorkItem)]
    return sorted(rows, key=_item_sort_key)[:MAX_WORK_ITEMS]


def _item_sort_key(item: GhostWorkItem) -> tuple[int, int, float, tuple[int, ...], str]:
    return (
        STATUS_PRIORITY.get(item.status, 99),
        KIND_PRIORITY.get(item.kind, 99),
        -item.priority,
        _reverse_text_sort_key(item.updated_at),
        item.id,
    )


def _claim_sort_key(
    item: GhostWorkItem, affinity_hints: Iterable[Any] = ()
) -> tuple[int, float, int, tuple[int, ...], str]:
    priority = apply_affinity_work_boost(item.priority, affinity_hints, item.id)
    return (
        SCOPE_PRIORITY.get(item.scope, 99),
        -priority,
        item.retry_count,
        _reverse_text_sort_key(item.updated_at),
        item.id,
    )


def _scope_matches(item: GhostWorkItem, *, project_ref: str, session_ref: str) -> bool:
    if item.scope == "session":
        return bool(session_ref and item.scope_ref == session_ref)
    if item.scope == "project":
        return bool(project_ref and _project_ref(item.scope_ref) == project_ref)
    return item.scope == "user"


def _scope_filter_matches(
    item: GhostWorkItem,
    *,
    scope: str,
    project_ref: str,
    session_ref: str,
) -> bool:
    normalized_scope = str(scope or "").strip().lower()
    if not normalized_scope:
        return (
            _scope_matches(item, project_ref=project_ref, session_ref=session_ref)
            if (project_ref or session_ref)
            else True
        )
    if item.scope != normalized_scope:
        return False
    if normalized_scope == "project":
        return bool(project_ref and _project_ref(item.scope_ref) == project_ref)
    if normalized_scope == "session":
        return bool(session_ref and item.scope_ref == session_ref)
    return True


def _items_from_events(events: Iterable[dict[str, object]]) -> list[GhostWorkItem]:
    by_id: dict[str, GhostWorkItem] = {}
    for event in events:
        event_type = str(event.get("type") or "")
        now = _event_ts(event)
        if event_type == "ghost_work_snapshot":
            by_id = {item.id: item for item in _snapshot_items(event)}
        elif event_type == "ghost_work_item_observed":
            item = GhostWorkItem.from_payload(event.get("item"))
            if item is None:
                continue
            item = replace(item, created_at=item.created_at or now, updated_at=item.updated_at or now)
            merged, _changed = _merge_items(by_id.values(), (item,), now=now)
            by_id = {row.id: row for row in _bounded_items(merged)}
        elif event_type == "ghost_work_item_transitioned":
            _apply_transition_event(by_id, event, now=now)
        elif event_type == "ghost_work_items_deleted":
            _apply_items_deleted_event(by_id, event)
    return list(by_id.values())


def _snapshot_items(event: Mapping[str, object]) -> list[GhostWorkItem]:
    return _bounded_items(
        item for item in (GhostWorkItem.from_payload(row) for row in _list(event.get("items"))) if item is not None
    )


def _projection_payload(
    items: Iterable[GhostWorkItem],
    *,
    generated_at: str,
    warnings: Iterable[str],
) -> dict[str, object]:
    rows = _bounded_items(items)
    return {
        "schema_version": WORK_QUEUE_SCHEMA_VERSION,
        "kind": _PROJECTION_KIND,
        "generated_at": generated_at,
        "items": [item.to_payload() for item in rows],
        "warnings": list(_bounded_warnings(warnings)),
    }


def _observed_event(item: GhostWorkItem, *, ts: str) -> dict[str, object]:
    return {
        "schema_version": WORK_QUEUE_SCHEMA_VERSION,
        "type": "ghost_work_item_observed",
        "event_id": "gwe_" + uuid.uuid4().hex[:24],
        "ts": clip_signal_text(ts, 80),
        "item": item.to_payload(),
    }


def _transition_event(
    current: GhostWorkItem,
    *,
    action: str,
    patch: Mapping[str, object],
    ts: str,
) -> dict[str, object]:
    return {
        "schema_version": WORK_QUEUE_SCHEMA_VERSION,
        "type": "ghost_work_item_transitioned",
        "event_id": "gwe_" + uuid.uuid4().hex[:24],
        "ts": clip_signal_text(ts, 80),
        "action": clip_signal_text(action, 40),
        "item_id": clip_signal_text(current.id, 120),
        "precondition": {
            "expected_status": current.status,
            "expected_started_run_id": current.started_run_id,
            "expected_retry_count": current.retry_count,
        },
        "patch": _transition_patch_payload(patch),
    }


def _items_deleted_event(
    *,
    reason: str,
    ts: str,
    item_ids: Iterable[object] = (),
    expected_items: Iterable[GhostWorkItem] = (),
    scope: str = "",
    project_ref: str = "",
    session_ref: str = "",
) -> dict[str, object]:
    expected = [
        {
            "id": item.id,
            "expected_status": item.status,
            "expected_started_run_id": item.started_run_id,
            "expected_retry_count": item.retry_count,
        }
        for item in expected_items
        if isinstance(item, GhostWorkItem)
    ]
    return {
        "schema_version": WORK_QUEUE_SCHEMA_VERSION,
        "type": "ghost_work_items_deleted",
        "event_id": "gwd_" + uuid.uuid4().hex[:24],
        "ts": clip_signal_text(ts, 80),
        "payload": {
            "reason": clip_signal_text(reason, 80),
            "item_ids": [item_id for item_id in (clip_signal_text(value, 120) for value in item_ids) if item_id],
            "expected_items": expected,
            "scope": _clean_scope(scope),
            "project_ref": clip_signal_text(project_ref, 120),
            "session_ref": clip_signal_text(session_ref, 120),
        },
    }


def _snapshot_event(
    items: Iterable[GhostWorkItem],
    *,
    ts: str,
    reason: str,
) -> dict[str, object]:
    rows = _bounded_items(items)
    return {
        "schema_version": WORK_QUEUE_SCHEMA_VERSION,
        "type": "ghost_work_snapshot",
        "event_id": "gws_" + uuid.uuid4().hex[:24],
        "ts": clip_signal_text(ts, 80),
        "reason": clip_signal_text(reason, 80),
        "items": [item.to_payload() for item in rows],
    }


def _valid_work_event(event: Mapping[str, object]) -> bool:
    if not clip_signal_text(event.get("event_id"), 120):
        return False
    if not clip_signal_text(event.get("ts"), 80):
        return False
    event_type = str(event.get("type") or "")
    if not _mapping_keys_within(event, _WORK_EVENT_KEYS.get(event_type, ())):
        return False
    if event_type == "ghost_work_snapshot":
        return _valid_work_snapshot(event)
    if event_type == "ghost_work_item_observed":
        return _valid_work_item_payload(event.get("item"))
    if event_type == "ghost_work_item_transitioned":
        return _valid_work_transition(event)
    if event_type == "ghost_work_items_deleted":
        payload = event.get("payload")
        if not _valid_work_delete_payload(payload):
            return False
        reason = str(payload.get("reason") or "") if isinstance(payload, Mapping) else ""
        item_ids = payload.get("item_ids") if isinstance(payload, Mapping) else []
        expected_items = payload.get("expected_items") if isinstance(payload, Mapping) else []
        scope = str(payload.get("scope") or "") if isinstance(payload, Mapping) else ""
        project_ref = str(payload.get("project_ref") or "") if isinstance(payload, Mapping) else ""
        session_ref = str(payload.get("session_ref") or "") if isinstance(payload, Mapping) else ""
        if reason == "expired":
            if item_ids or scope or project_ref or session_ref:
                return False
            return bool(expected_items) and all(
                _valid_work_precondition(row, require_id=True) for row in expected_items
            )
        if reason == "scope_deleted":
            if item_ids or expected_items:
                return False
            if scope == "user":
                return not project_ref and not session_ref
            if scope == "project":
                return bool(project_ref and not session_ref)
            if scope == "session":
                return bool(session_ref and not project_ref)
            return False
        return False
    return False


def _valid_work_transition(event: Mapping[str, object]) -> bool:
    item_id = clip_signal_text(event.get("item_id"), 120)
    action = clip_signal_text(event.get("action"), 40)
    precondition = event.get("precondition")
    patch = event.get("patch")
    if not item_id or action not in WORK_ITEM_TRANSITION_ACTIONS:
        return False
    if not _valid_work_precondition(precondition) or not isinstance(patch, Mapping):
        return False
    if not _mapping_keys_within(patch, _WORK_TRANSITION_PATCH_KEYS.get(action, ())):
        return False
    target_status = _clean_status(patch.get("status"))
    if not target_status:
        return False
    updated_at = clip_signal_text(patch.get("updated_at"), 80)
    if not updated_at:
        return False

    expected_status = _clean_status(precondition.get("expected_status"))
    expected_started_run_id = clip_signal_text(precondition.get("expected_started_run_id"), 120)
    expected_retry_count = precondition.get("expected_retry_count")
    if not _valid_nonnegative_int_payload(expected_retry_count):
        return False

    if action == "claim":
        if target_status != "running" or expected_status != "queued":
            return False
        if expected_started_run_id:
            return False
        started_run_id = clip_signal_text(patch.get("started_run_id"), 120)
        lease_expires_at = clip_signal_text(patch.get("lease_expires_at"), 80)
        if not started_run_id or not lease_expires_at:
            return False
        if "retry_count" not in patch:
            return False
        retry_count = patch["retry_count"]
        if not _valid_nonnegative_int_payload(retry_count):
            return False
        if retry_count < 1 or retry_count != expected_retry_count + 1:
            return False
        if clip_signal_text(patch.get("blocked_reason"), 120):
            return False
        if clip_signal_text(patch.get("completed_run_id"), 120):
            return False
        if _bounded_refs(patch.get("proof_refs")):
            return False
        return True

    if action == "complete":
        if target_status != "done" or expected_status != "running":
            return False
        completed_run_id = clip_signal_text(patch.get("completed_run_id"), 120)
        if not expected_started_run_id or not completed_run_id or completed_run_id != expected_started_run_id:
            return False
        proof_refs = _bounded_refs(patch.get("proof_refs"))
        if not proof_refs:
            return False
        if clip_signal_text(patch.get("lease_expires_at"), 80):
            return False
        if clip_signal_text(patch.get("blocked_reason"), 120):
            return False
        return True

    if action in {"release", "release_stale"}:
        if target_status not in {"queued", "blocked"} or expected_status != "running":
            return False
        if not expected_started_run_id:
            return False
        if clip_signal_text(patch.get("lease_expires_at"), 80):
            return False
        if clip_signal_text(patch.get("completed_run_id"), 120):
            return False
        if _bounded_refs(patch.get("proof_refs")):
            return False
        if target_status == "queued":
            if clip_signal_text(patch.get("started_run_id"), 120):
                return False
            if clip_signal_text(patch.get("blocked_reason"), 120):
                return False
        elif target_status == "blocked":
            if not clip_signal_text(patch.get("blocked_reason"), 120):
                return False
            started_run_id = clip_signal_text(patch.get("started_run_id"), 120)
            if started_run_id and started_run_id != expected_started_run_id:
                return False
        return True

    if action == "block":
        if target_status != "blocked" or expected_status not in {"running", "queued", "candidate"}:
            return False
        if not clip_signal_text(patch.get("blocked_reason"), 120):
            return False
        if clip_signal_text(patch.get("lease_expires_at"), 80):
            return False
        if clip_signal_text(patch.get("completed_run_id"), 120):
            return False
        if _bounded_refs(patch.get("proof_refs")):
            return False
        return True

    if action == "reject":
        if target_status != "rejected" or expected_status not in {"candidate", "queued", "blocked"}:
            return False
        if clip_signal_text(patch.get("lease_expires_at"), 80):
            return False
        if clip_signal_text(patch.get("blocked_reason"), 120):
            return False
        if clip_signal_text(patch.get("started_run_id"), 120):
            return False
        if clip_signal_text(patch.get("completed_run_id"), 120):
            return False
        if _bounded_refs(patch.get("proof_refs")):
            return False
        return True

    if action == "queue":
        if target_status != "queued" or expected_status not in {"candidate", "blocked", "rejected"}:
            return False
        if "retry_count" not in patch:
            return False
        retry_count = patch["retry_count"]
        if not _valid_nonnegative_int_payload(retry_count) or retry_count != 0:
            return False
        if clip_signal_text(patch.get("started_run_id"), 120):
            return False
        if clip_signal_text(patch.get("completed_run_id"), 120):
            return False
        if _bounded_refs(patch.get("proof_refs")):
            return False
        if clip_signal_text(patch.get("blocked_reason"), 120):
            return False
        if clip_signal_text(patch.get("lease_expires_at"), 80):
            return False
        return True

    return False


def _valid_work_snapshot(event: Mapping[str, object]) -> bool:
    raw_items = event.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > MAX_WORK_ITEMS:
        return False
    item_ids: set[str] = set()
    for row in raw_items:
        item = GhostWorkItem.from_payload(row)
        if item is None or item.id in item_ids:
            return False
        if not _valid_work_item_payload(row):
            return False
        item_ids.add(item.id)
    return True


def _valid_work_precondition(value: object, *, require_id: bool = False) -> bool:
    if not isinstance(value, Mapping):
        return False
    allowed = _WORK_PRECONDITION_KEYS | (frozenset({"id"}) if require_id else frozenset())
    if set(value.keys()) != allowed:
        return False
    if require_id and not _valid_canonical_text(value.get("id"), 120, required=True):
        return False
    expected_status = value.get("expected_status")
    if not isinstance(expected_status, str) or _clean_status(expected_status) != expected_status:
        return False
    if not _valid_canonical_text(value.get("expected_started_run_id"), 120):
        return False
    return _valid_nonnegative_int_payload(value.get("expected_retry_count"))


def _event_ts(event: Mapping[str, object]) -> str:
    return clip_signal_text(event.get("ts"), 80)


def _transition_patch_payload(patch: Mapping[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    if "status" in patch:
        status = _clean_status(patch.get("status"))
        if status:
            out["status"] = status
    if "started_run_id" in patch:
        out["started_run_id"] = clip_signal_text(patch.get("started_run_id"), 120)
    if "completed_run_id" in patch:
        out["completed_run_id"] = clip_signal_text(patch.get("completed_run_id"), 120)
    if "proof_refs" in patch:
        out["proof_refs"] = list(_bounded_refs(patch.get("proof_refs")))
    if "blocked_reason" in patch:
        out["blocked_reason"] = clip_signal_text(patch.get("blocked_reason"), 120)
    if "retry_count" in patch:
        out["retry_count"] = max(0, _int(patch.get("retry_count")))
    if "lease_expires_at" in patch:
        out["lease_expires_at"] = clip_signal_text(patch.get("lease_expires_at"), 80)
    if "updated_at" in patch:
        out["updated_at"] = clip_signal_text(patch.get("updated_at"), 80)
    return out


def _apply_transition_event(
    by_id: dict[str, GhostWorkItem],
    event: Mapping[str, object],
    *,
    now: str,
) -> str:
    item_id = clip_signal_text(event.get("item_id"), 120)
    current = by_id.get(item_id)
    if current is None:
        return "stale"
    precondition = event.get("precondition")
    patch = event.get("patch")
    if not isinstance(precondition, Mapping) or not isinstance(patch, Mapping):
        return "invalid"
    if not _precondition_matches(current, precondition):
        return "stale"
    target_status = _clean_status(patch.get("status"))
    action = clip_signal_text(event.get("action"), 40)
    if not target_status or not _transition_allowed(current, target_status, action=action):
        return "invalid"

    if action == "claim":
        started_run_id = clip_signal_text(patch.get("started_run_id"), 120)
        lease_expires_at = clip_signal_text(patch.get("lease_expires_at"), 80)
        if not started_run_id or not lease_expires_at or "retry_count" not in patch:
            return "invalid"
        retry_count = patch["retry_count"]
        if not _valid_nonnegative_int_payload(retry_count):
            return "invalid"
        if retry_count < 1 or retry_count != current.retry_count + 1:
            return "invalid"
        updated = replace(
            current,
            status="running",
            started_run_id=started_run_id,
            retry_count=retry_count,
            lease_expires_at=lease_expires_at,
            completed_run_id="",
            proof_refs=(),
            blocked_reason="",
            updated_at=clip_signal_text(patch.get("updated_at"), 80) or now,
        )
    elif action == "complete":
        completed_run_id = clip_signal_text(patch.get("completed_run_id"), 120)
        if not completed_run_id or not current.started_run_id or completed_run_id != current.started_run_id:
            return "invalid"
        proof_refs = _bounded_refs(patch.get("proof_refs"))
        if not proof_refs or not _primary_proof_matches_item_kind(current, proof_refs):
            return "invalid"
        updated = replace(
            current,
            status="done",
            completed_run_id=completed_run_id,
            proof_refs=proof_refs,
            blocked_reason="",
            lease_expires_at="",
            updated_at=clip_signal_text(patch.get("updated_at"), 80) or now,
        )
    elif action in {"release", "release_stale"}:
        blocked_reason = clip_signal_text(patch.get("blocked_reason"), 120) if target_status == "blocked" else ""
        if target_status == "blocked" and not blocked_reason:
            return "invalid"
        updated = replace(
            current,
            status=target_status,
            started_run_id="" if target_status == "queued" else current.started_run_id,
            completed_run_id="",
            proof_refs=(),
            lease_expires_at="",
            blocked_reason=blocked_reason,
            updated_at=clip_signal_text(patch.get("updated_at"), 80) or now,
        )
    elif action == "block":
        blocked_reason = clip_signal_text(patch.get("blocked_reason"), 120)
        if not blocked_reason:
            return "invalid"
        updated = replace(
            current,
            status="blocked",
            blocked_reason=blocked_reason,
            completed_run_id="",
            proof_refs=(),
            lease_expires_at="",
            updated_at=clip_signal_text(patch.get("updated_at"), 80) or now,
        )
    elif action == "reject":
        updated = replace(
            current,
            status="rejected",
            started_run_id="",
            completed_run_id="",
            proof_refs=(),
            lease_expires_at="",
            blocked_reason="",
            updated_at=clip_signal_text(patch.get("updated_at"), 80) or now,
        )
    elif action == "queue":
        if "retry_count" not in patch:
            return "invalid"
        try:
            if int(patch["retry_count"]) != 0:
                return "invalid"
        except (TypeError, ValueError):
            return "invalid"
        updated = replace(
            current,
            status="queued",
            retry_count=0,
            started_run_id="",
            completed_run_id="",
            proof_refs=(),
            blocked_reason="",
            lease_expires_at="",
            updated_at=clip_signal_text(patch.get("updated_at"), 80) or now,
        )
    else:
        return "invalid"

    if GhostWorkItem.from_payload(updated.to_payload()) is None:
        return "invalid"
    by_id[updated.id] = updated
    return "applied"


def _apply_items_deleted_event(by_id: dict[str, GhostWorkItem], event: Mapping[str, object]) -> None:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return
    item_ids = {
        item_id for item_id in (clip_signal_text(value, 120) for value in _list(payload.get("item_ids"))) if item_id
    }
    expected_items = [row for row in _list(payload.get("expected_items")) if isinstance(row, Mapping)]
    if expected_items:
        for row in expected_items:
            item_id = clip_signal_text(row.get("id"), 120)
            current = by_id.get(item_id)
            if current is not None and _precondition_matches(current, row):
                by_id.pop(item_id, None)
        return
    if item_ids:
        for item_id in item_ids:
            by_id.pop(item_id, None)
        return
    scope = _clean_scope(payload.get("scope"))
    project_ref = clip_signal_text(payload.get("project_ref"), 120)
    session_ref = clip_signal_text(payload.get("session_ref"), 120)
    if not scope:
        return
    for item_id, item in list(by_id.items()):
        if _scope_filter_matches(item, scope=scope, project_ref=project_ref, session_ref=session_ref):
            by_id.pop(item_id, None)


def _precondition_matches(item: GhostWorkItem, precondition: Mapping[str, object]) -> bool:
    expected_status = _clean_status(precondition.get("expected_status"))
    if expected_status and item.status != expected_status:
        return False
    if "expected_started_run_id" in precondition:
        expected_run_id = clip_signal_text(precondition.get("expected_started_run_id"), 120)
        if item.started_run_id != expected_run_id:
            return False
    if "expected_retry_count" in precondition and item.retry_count != max(
        0, _int(precondition.get("expected_retry_count"))
    ):
        return False
    return True


def _transition_allowed(item: GhostWorkItem, target_status: str, *, action: str) -> bool:
    return target_status in WORK_ITEM_TRANSITION_MATRIX.get(action, {}).get(item.status, frozenset())


def _stable_item_id(
    *,
    kind: str,
    scope: str,
    scope_ref: str,
    source: str,
    source_ref: str,
    title: str,
) -> str:
    key = "|".join(
        (
            kind,
            scope,
            scope_ref,
            source,
            source_ref,
            " ".join(str(title or "").split()).casefold(),
        )
    )
    return "gwi_" + hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:24]


def _clean_item_text(value: object, *, max_chars: int) -> str:
    text = " ".join(str(value or "").replace("\r\n", "\n").replace("\r", "\n").split())
    text = clip_signal_text(text, max_chars).rstrip(".")
    if not text:
        return ""
    lower = text.casefold()
    if "ghost" in lower or "work queue" in lower or "workitem" in lower:
        return ""
    if contains_sensitive_signal_text(text) or not is_prompt_visible_text_safe(text):
        return ""
    return text


def _clean_kind(value: object) -> str:
    kind = str(value or "").strip().lower()
    return kind if kind in WORK_ITEM_KINDS else ""


def _clean_status(value: object) -> str:
    status = str(value or "").strip().lower()
    return status if status in WORK_ITEM_STATUSES else ""


def _clean_scope(value: object) -> str:
    scope = str(value or "").strip().lower()
    return scope if scope in {"user", "project", "session"} else ""


def _clean_source(value: object) -> str:
    source = str(value or "").strip().lower()
    return source if source in WORK_ITEM_SOURCES else ""


def _bounded_refs(values: object) -> tuple[str, ...]:
    out: list[str] = []
    for value in _list(values):
        text = clip_signal_text(value, MAX_WORK_REF_CHARS)
        if not text or contains_sensitive_signal_text(text):
            continue
        if text not in out:
            out.append(text)
        if len(out) >= MAX_WORK_REFS:
            break
    return tuple(out)


def _proof_run_ref(run_id: str) -> str:
    text = clip_signal_text(run_id, 120)
    if not text:
        return ""
    if contains_sensitive_signal_text(text):
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    return text


def _receipt_proves_project_work(receipt: Mapping[str, object]) -> bool:
    work = receipt.get("work")
    return isinstance(work, Mapping) and _int(work.get("changed_count")) > 0


def _primary_proof_matches_item_kind(item: GhostWorkItem, refs: Iterable[str]) -> bool:
    prefixes = {str(ref).split(":", 1)[0] for ref in refs}
    if item.kind in {"research", "open_question"}:
        return any(_research_proof_ref(ref) for ref in refs)
    if item.kind == "review":
        return "review" in prefixes
    if item.kind in {"coding", "project_followup"}:
        return bool(prefixes.intersection({"diff", "receipt"}))
    return False


def _research_proof_ref(value: object) -> str:
    text = str(value or "").strip()
    prefix = "research_proof:"
    if not text.startswith(prefix):
        return ""
    suffix = text.removeprefix(prefix)
    if len(suffix) == 16 and all(ch in "0123456789abcdef" for ch in suffix):
        return text
    return ""


def _clean_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, object] = {}
    for key, item in value.items():
        clean_key = clip_signal_text(key, 80)
        if not clean_key or contains_sensitive_signal_text(clean_key):
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            clean_item: object = clip_signal_text(item, 180) if isinstance(item, str) else item
        else:
            clean_item = clip_signal_text(item, 180)
        if isinstance(clean_item, str) and contains_sensitive_signal_text(clean_item):
            continue
        out[clean_key] = clean_item
        if len(out) >= 12:
            break
    return out


def _filter_values(value: object, allowed: frozenset[str]) -> set[str]:
    values = {str(item).strip().lower() for item in str(value or "").split(",") if str(item).strip()}
    return {item for item in values if item in allowed}


def _normalize_project(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return clip_signal_text(Path(text).expanduser().resolve(), 240)
    except (OSError, RuntimeError, ValueError):
        return clip_signal_text(text, 240)


def _project_ref(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return project_key(text)
    except (OSError, RuntimeError, ValueError):
        return hashlib.sha256(text.casefold().encode("utf-8", errors="replace")).hexdigest()[:24]


def _session_ref(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return session_key(text)


def _is_expired(item: GhostWorkItem, now: str) -> bool:
    if not item.expires_at:
        return False
    return _parse_ts(item.expires_at) <= _parse_ts(now)


def _is_stale_claim(item: GhostWorkItem, now: str) -> bool:
    if item.status != "running":
        return False
    if not item.lease_expires_at:
        return True
    lease_expires_at = _parse_ts_or_none(item.lease_expires_at)
    if lease_expires_at is None:
        return True
    return lease_expires_at <= _parse_ts(now)


def _parse_ts(value: object) -> datetime:
    return _parse_ts_or_none(value) or datetime.now(timezone.utc)


def _parse_ts_or_none(value: object) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _unit_float(value: object) -> float:
    return clamp_unit_float(value, digits=4)


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _list(value: object) -> list[object]:
    if isinstance(value, tuple):
        return list(value)
    return value if isinstance(value, list) else []


def _field(value: Any, key: str) -> object:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, "")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _future_ts(now: str, seconds: int) -> str:
    base = _parse_ts(now)
    try:
        delta_seconds = int(seconds)
    except (TypeError, ValueError):
        delta_seconds = DEFAULT_WORK_CLAIM_LEASE_SECONDS
    return (
        datetime.fromtimestamp(
            base.timestamp() + delta_seconds,
            tz=timezone.utc,
        )
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _bounded_warnings(warnings: Iterable[object]) -> tuple[str, ...]:
    out: list[str] = []
    for warning in warnings:
        text = clip_signal_text(warning, 180)
        if not text or contains_sensitive_signal_text(text):
            text = "redacted_warning"
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_WORK_WARNINGS:
            break
    return tuple(out)


def _event_read_warnings(warnings: Iterable[str]) -> tuple[str, ...]:
    mapped: list[str] = []
    for warning in warnings:
        if warning == "work_events.jsonl:too_large":
            mapped.append("work_events_too_large")
        elif warning == "work_events.jsonl:unreadable":
            mapped.append("work_events_unreadable")
        else:
            mapped.append(str(warning))
    return _bounded_warnings(mapped)


def _valid_work_item_payload(payload: object) -> bool:
    item = GhostWorkItem.from_payload(payload)
    return item is not None and _strict_payload_equal(payload, item.to_payload())


def _valid_work_delete_payload(payload: object) -> bool:
    if not isinstance(payload, Mapping) or set(payload.keys()) != _WORK_DELETE_PAYLOAD_KEYS:
        return False
    reason = payload.get("reason")
    if not isinstance(reason, str) or reason not in {"expired", "scope_deleted"}:
        return False
    item_ids = payload.get("item_ids")
    expected_items = payload.get("expected_items")
    if not _valid_ref_list_payload(item_ids) or not isinstance(expected_items, list):
        return False
    if not all(_valid_work_precondition(row, require_id=True) for row in expected_items):
        return False
    scope = payload.get("scope")
    project_ref = payload.get("project_ref")
    session_ref = payload.get("session_ref")
    if not _valid_scope_payload(scope):
        return False
    if not _valid_canonical_text(project_ref, 120) or not _valid_canonical_text(session_ref, 120):
        return False
    if reason == "expired":
        return item_ids == [] and bool(expected_items) and scope == "" and project_ref == "" and session_ref == ""
    if item_ids or expected_items:
        return False
    if scope == "user":
        return project_ref == "" and session_ref == ""
    if scope == "project":
        return bool(project_ref) and session_ref == ""
    if scope == "session":
        return bool(session_ref) and project_ref == ""
    return False


def _valid_ref_list_payload(value: object) -> bool:
    return isinstance(value, list) and list(_bounded_refs(value)) == value


def _valid_scope_payload(value: object) -> bool:
    return isinstance(value, str) and (value == "" or _clean_scope(value) == value)


def _valid_canonical_text(value: object, limit: int, *, required: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    if required and not value:
        return False
    return clip_signal_text(value, limit) == value and not contains_sensitive_signal_text(value)


def _valid_nonnegative_int_payload(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _strict_payload_equal(value: object, expected: object) -> bool:
    if isinstance(expected, Mapping):
        if not isinstance(value, Mapping) or set(value.keys()) != set(expected.keys()):
            return False
        return all(_strict_payload_equal(value[key], expected[key]) for key in expected)
    if isinstance(expected, list):
        if not isinstance(value, list) or len(value) != len(expected):
            return False
        return all(
            _strict_payload_equal(item, expected_item) for item, expected_item in zip(value, expected, strict=True)
        )
    return type(value) is type(expected) and value == expected


def _mapping_keys_within(value: Mapping[str, object], allowed: Iterable[str]) -> bool:
    allowed_keys = set(allowed)
    return all(isinstance(key, str) and key in allowed_keys for key in value.keys())


def _meaningful_item_payload(item: GhostWorkItem) -> tuple[object, ...]:
    return (
        item.kind,
        item.status,
        item.scope,
        item.scope_ref,
        item.title,
        item.why_now,
        round(item.priority, 6),
        round(item.confidence, 6),
        item.source,
        item.source_ref,
        item.evidence_refs,
        item.run_refs,
        item.started_run_id,
        item.completed_run_id,
        item.proof_refs,
        item.blocked_reason,
        item.retry_count,
        item.lease_expires_at,
        tuple(sorted(_clean_metadata(item.metadata).items())),
    )


def _item_payloads(items: Iterable[GhostWorkItem]) -> tuple[tuple[object, ...], ...]:
    return tuple(_meaningful_item_payload(item) for item in _bounded_items(items))


def _reverse_text_sort_key(value: object) -> tuple[int, ...]:
    return tuple(-ord(ch) for ch in str(value or ""))


def _normalize_continuation_text(value: object) -> str:
    text = " ".join(str(value or "").casefold().strip().split())
    return text.strip(" 。.!！?？")


__all__ = [
    "GhostWorkClaimResult",
    "GhostWorkItem",
    "GhostWorkQueueStore",
    "GhostWorkSyncResult",
    "WORK_QUEUE_SCHEMA_VERSION",
    "is_strict_work_continuation",
    "mode_for_work_item",
    "proof_refs_from_task_event",
    "render_work_item_task",
]
