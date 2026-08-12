"""Bounded local work-item queue for Ghost continuity.

The queue is a local state machine, not an autonomous runner. It can remember
audited follow-up work and claim one item when the user explicitly asks to
continue, but TaskRunner remains the only execution entry point.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping
import uuid

from codey.ghost.continuity import GhostContinuityStore
from codey.ghost.schema import clip_signal_text, contains_sensitive_signal_text
from codey.ghost.typed_fields import dangerous_text, safe_rendered_body
from codey.local_store import DEFAULT_STATE_HOME, delete_file, project_key, read_json, session_key, write_json_atomic


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

WORK_ITEM_KINDS = frozenset({
    "research",
    "coding",
    "review",
    "memory_sleep",
    "open_question",
    "project_followup",
})
WORK_ITEM_STATUSES = frozenset({
    "candidate",
    "queued",
    "running",
    "blocked",
    "done",
    "rejected",
    "expired",
})
WORK_ITEM_SOURCES = frozenset({
    "continuity",
    "research_note",
    "run_ledger",
    "review",
    "work_checkpoint",
    "sleep",
    "user",
})
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

_STRICT_CONTINUATION_CN = frozenset({
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
})
_STRICT_CONTINUATION_EN = frozenset({
    "continue",
    "continue please",
    "next",
    "next item",
    "handle pending item",
    "continue pending item",
    "continue saved task",
    "resume saved task",
    "resume queued task",
})


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
        if status == "running" and not clip_signal_text(payload.get("started_run_id"), 120):
            return None
        if status == "done" and not _bounded_refs(payload.get("proof_refs")):
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
            started_run_id=clip_signal_text(payload.get("started_run_id"), 120),
            completed_run_id=clip_signal_text(payload.get("completed_run_id"), 120),
            proof_refs=_bounded_refs(payload.get("proof_refs")),
            blocked_reason=clip_signal_text(payload.get("blocked_reason"), 120),
            retry_count=max(0, _int(payload.get("retry_count"))),
            lease_expires_at=clip_signal_text(payload.get("lease_expires_at"), 80),
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


class GhostWorkQueueStore:
    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.directory = Path(state_home) / "ghost"
        self.projection_path = self.directory / "work_items.json"
        self.events_path = self.directory / "work_events.jsonl"
        self.last_warnings: tuple[str, ...] = ()
        self._events_read_blocked = False

    def sync_from_sources(
        self,
        *,
        continuity_store: GhostContinuityStore | None = None,
        work_checkpoint_store: Any = None,
        run_projection: Any = None,
        terminal_event: Mapping[str, object] | None = None,
        session_id: str = "",
        run_id: str = "",
        project: str = "",
    ) -> GhostWorkSyncResult:
        try:
            existing = list(self._load_items_for_event_rewrite())
            if self._events_read_blocked:
                return self._sync_failed("events_read_blocked")
            now = _now()
            candidates: list[GhostWorkItem] = []
            candidates.extend(_items_from_continuity(
                continuity_store,
                session_id=session_id,
                project=project,
                now=now,
            ))
            candidates.extend(_items_from_work_checkpoint(
                work_checkpoint_store,
                session_id=session_id,
                project=project,
                now=now,
            ))
            candidates.extend(_items_from_run_projection(
                run_projection,
                session_id=session_id,
                project=project,
                now=now,
            ))
            candidates.extend(_items_from_terminal_event(
                terminal_event,
                session_id=session_id,
                run_id=run_id,
                project=project,
                now=now,
            ))
            if not candidates:
                active = _bounded_items(item for item in existing if not _is_expired(item, now))
                if len(active) != len(existing):
                    if not self._replace_items(active, "ghost_work_items_expired"):
                        return self._sync_failed("event_write_failed")
                self.last_warnings = ()
                return GhostWorkSyncResult(True, skipped_reason="no_sources", total_items=len(active))
            active_existing = _bounded_items(item for item in existing if not _is_expired(item, now))
            merged, changed = _merge_items(active_existing, candidates, now=now)
            merged = _bounded_items(item for item in merged if not _is_expired(item, now))
            projection_changed = _item_payloads(active_existing) != _item_payloads(merged)
            if not changed and not projection_changed:
                self.last_warnings = ()
                return GhostWorkSyncResult(True, items_changed=0, total_items=len(merged))
            events: list[dict[str, object]] = []
            if changed:
                events = [_item_event(item, "upsert") for item in changed]
                events.append(_control_event(
                    "ghost_work_items_synced",
                    {
                        "session_ref": _session_ref(session_id),
                        "project_ref": _project_ref(project),
                        "run_id": clip_signal_text(run_id, 120),
                        "items_seen": len(candidates),
                        "items_changed": len(changed),
                        "items_total": len(merged),
                    },
                ))
            if events and not self._append_events_atomic(events):
                return self._sync_failed("event_write_failed")
            elif projection_changed:
                if not self._replace_items(merged, "ghost_work_items_pruned"):
                    return self._sync_failed("event_write_failed")
            self._write_projection_best_effort(merged)
            self._compact_if_needed(merged)
            return GhostWorkSyncResult(True, items_changed=len(changed), total_items=len(merged), warnings=self.last_warnings)
        except (OSError, TypeError, ValueError):
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
        statuses = _filter_values(status, WORK_ITEM_STATUSES)
        kinds = _filter_values(kind, WORK_ITEM_KINDS)
        project_ref = _project_ref(project)
        session_ref = _session_ref(session_id)
        rows = []
        now = _now()
        for item in self._load_items():
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
    ) -> GhostWorkClaimResult:
        if not clip_signal_text(run_id, 120):
            return GhostWorkClaimResult(False, skipped_reason="run_id_required")
        if not is_strict_work_continuation(user_request):
            return GhostWorkClaimResult(False, skipped_reason="not_continuation")
        try:
            items = list(self._load_items_for_event_rewrite())
            if self._events_read_blocked:
                return GhostWorkClaimResult(False, skipped_reason="events_read_blocked", warnings=self.last_warnings)
            now = _now()
            items, stale = _release_stale_claims(items, now=now)
            if stale and not self._append_events_atomic([_item_event(item, "release_stale") for item in stale]):
                return GhostWorkClaimResult(False, skipped_reason="event_write_failed", warnings=self.last_warnings)
            candidate = _next_claimable_item(items, session_id=session_id, project=project)
            if candidate is None:
                if stale:
                    self._write_projection_best_effort(items)
                    self._compact_if_needed(items)
                return GhostWorkClaimResult(False, skipped_reason="no_queued_item")
            mode = mode_for_work_item(replace(candidate, status="running"), project=project)
            if not mode:
                return GhostWorkClaimResult(False, skipped_reason="unrunnable_item")
            claimed = replace(
                candidate,
                status="running",
                started_run_id=clip_signal_text(run_id, 120),
                retry_count=candidate.retry_count + 1,
                lease_expires_at=_future_ts(now, lease_seconds),
                updated_at=now,
                blocked_reason="",
            )
            updated = _replace_item(items, claimed)
            if not self._append_events_atomic([_item_event(claimed, "claim")]):
                return GhostWorkClaimResult(False, skipped_reason="event_write_failed", warnings=self.last_warnings)
            self._write_projection_best_effort(updated)
            self._compact_if_needed(updated)
            return GhostWorkClaimResult(
                True,
                item=claimed,
                mode=mode,
                task=render_work_item_task(claimed, user_request=user_request),
                warnings=self.last_warnings,
            )
        except (OSError, TypeError, ValueError):
            return GhostWorkClaimResult(False, skipped_reason="work_queue_error")

    def complete_item(
        self,
        item_id: str,
        *,
        run_id: str,
        proof_refs: Iterable[object],
    ) -> GhostWorkItem | None:
        refs = _bounded_refs(tuple(proof_refs))
        if not refs:
            return self.block_item(item_id, run_id=run_id, blocked_reason="missing_proof")
        items = list(self._load_items_for_event_rewrite())
        if self._events_read_blocked:
            raise OSError("ghost work events are unreadable")
        current = _find_item(items, item_id)
        expected_run_id = clip_signal_text(run_id, 120)
        if current is None or current.status != "running":
            return None
        if expected_run_id and current.started_run_id and current.started_run_id != expected_run_id:
            return None
        if not _primary_proof_matches_item_kind(current, refs):
            return self.block_item(item_id, run_id=run_id, blocked_reason="missing_proof")
        return self._transition_item(
            item_id,
            expected_run_id=run_id,
            status="done",
            proof_refs=refs,
            completed_run_id=clip_signal_text(run_id, 120),
            blocked_reason="",
            action="complete",
        )

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
        items = list(self._load_items_for_event_rewrite())
        if self._events_read_blocked:
            raise OSError("ghost work events are unreadable")
        current = _find_item(items, item_id)
        if current is None:
            return None
        if current.status != "running":
            return None
        if run_id and current.started_run_id and current.started_run_id != clip_signal_text(run_id, 120):
            return None
        next_status = "blocked" if current.retry_count >= MAX_WORK_RETRIES else "queued"
        updated = replace(
            current,
            status=next_status,
            started_run_id="" if next_status == "queued" else current.started_run_id,
            lease_expires_at="",
            blocked_reason=clip_signal_text(reason or "retry_limit" if next_status == "blocked" else "", 120),
            updated_at=_now(),
        )
        return self._store_transition(items, updated, action="release")

    def reject_item(self, item_id: str) -> GhostWorkItem | None:
        return self._transition_item(item_id, status="rejected", blocked_reason="", action="reject")

    def queue_item(self, item_id: str) -> GhostWorkItem | None:
        items = list(self._load_items_for_event_rewrite())
        if self._events_read_blocked:
            raise OSError("ghost work events are unreadable")
        current = _find_item(items, item_id)
        if current is None:
            return None
        if current.status == "queued":
            return current
        if current.status not in {"candidate", "blocked", "rejected"}:
            return None
        queued = replace(
            current,
            status="queued",
            started_run_id="",
            completed_run_id="",
            proof_refs=(),
            blocked_reason="",
            lease_expires_at="",
            updated_at=_now(),
        )
        return self._store_transition(items, queued, action="queue")

    def reconcile_stale_claims(self) -> GhostWorkSyncResult:
        try:
            items = list(self._load_items_for_event_rewrite())
            if self._events_read_blocked:
                return self._sync_failed("events_read_blocked")
            updated, stale = _release_stale_claims(items, now=_now())
            if not stale:
                self.last_warnings = ()
                return GhostWorkSyncResult(True, skipped_reason="no_stale_claims", total_items=len(updated))
            if not self._append_events_atomic([_item_event(item, "release_stale") for item in stale]):
                return self._sync_failed("event_write_failed")
            self._write_projection_best_effort(updated)
            self._compact_if_needed(updated)
            return GhostWorkSyncResult(True, items_changed=len(stale), total_items=len(updated), warnings=self.last_warnings)
        except (OSError, TypeError, ValueError):
            return self._sync_failed("work_queue_error")

    def export_state(self) -> dict[str, object]:
        events_missing = not self.events_path.is_file()
        events = self._read_events()
        event_warnings = self.last_warnings
        if self._events_read_blocked:
            items = self._load_projection_items()
        elif events_missing:
            items = self._load_projection_items()
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
            delete_file(self.projection_path)
            delete_file(self.events_path)
            return True
        except OSError:
            return False

    def delete_scope(
        self,
        scope: str,
        *,
        project: str = "",
        session_id: str = "",
    ) -> int:
        normalized_scope = _clean_scope(scope)
        if not normalized_scope:
            raise ValueError("scope must be user, project, or session")
        project_ref = _project_ref(project)
        session_ref = _session_ref(session_id)
        if normalized_scope == "project" and not project_ref:
            raise ValueError("project is required for project scope deletion")
        if normalized_scope == "session" and not session_ref:
            raise ValueError("session_id is required for session scope deletion")
        items = list(self._load_items_for_event_rewrite())
        if self._events_read_blocked:
            raise OSError("ghost work events are unreadable")
        kept = [
            item for item in items
            if not _scope_filter_matches(
                item,
                scope=normalized_scope,
                project_ref=project_ref,
                session_ref=session_ref,
            )
        ]
        removed = len(items) - len(kept)
        if not removed:
            return 0
        self._rewrite_events_from_items(
            kept,
            control_event=_control_event(
                "ghost_work_scope_deleted",
                {
                    "scope": normalized_scope,
                    "project_ref": project_ref if normalized_scope == "project" else "",
                    "session_ref": session_ref if normalized_scope == "session" else "",
                    "removed_count": removed,
                },
            ),
        )
        self._write_projection(kept, warnings=[])
        return removed

    def rebuild_from_events(self) -> bool:
        try:
            events = self._read_events()
            if self._events_read_blocked:
                return False
            self._write_projection(_bounded_items(_items_from_events(events)), warnings=[])
            return True
        except (OSError, TypeError, ValueError):
            return False

    def compact_if_needed(self) -> dict[str, object]:
        before = _event_file_stats(self.events_path, max_bytes=MAX_WORK_EVENTS_BYTES)
        if not before["readable"]:
            warning = str(before["warning"] or "work_events_unreadable")
            self.last_warnings = (warning,)
            return _compact_payload(False, False, before, before, (warning,))
        if before["events"] <= MAX_WORK_EVENTS and before["bytes"] <= MAX_WORK_EVENTS_BYTES:
            return _compact_payload(True, False, before, before, self.last_warnings)
        items = self._load_items_for_event_rewrite()
        if self._events_read_blocked:
            return _compact_payload(False, False, before, before, self.last_warnings)
        self._rewrite_events_from_items(items)
        after = _event_file_stats(self.events_path, max_bytes=MAX_WORK_EVENTS_BYTES)
        return _compact_payload(True, after != before, before, after, self.last_warnings)

    def _transition_item(
        self,
        item_id: str,
        *,
        status: str,
        expected_run_id: str = "",
        proof_refs: tuple[str, ...] = (),
        completed_run_id: str = "",
        blocked_reason: str = "",
        action: str,
    ) -> GhostWorkItem | None:
        items = list(self._load_items_for_event_rewrite())
        if self._events_read_blocked:
            raise OSError("ghost work events are unreadable")
        current = _find_item(items, item_id)
        if current is None:
            return None
        if status == "done" and current.status != "running":
            return None
        if status == "blocked" and current.status not in {"running", "queued", "candidate"}:
            return None
        if expected_run_id and current.started_run_id and current.started_run_id != clip_signal_text(expected_run_id, 120):
            return None
        updated = replace(
            current,
            status=_clean_status(status) or current.status,
            started_run_id="" if status in {"queued", "candidate"} else current.started_run_id,
            completed_run_id=completed_run_id or current.completed_run_id,
            proof_refs=proof_refs or current.proof_refs,
            blocked_reason=blocked_reason,
            lease_expires_at=current.lease_expires_at if status == "running" else "",
            updated_at=_now(),
        )
        return self._store_transition(items, updated, action=action)

    def _store_transition(
        self,
        items: Iterable[GhostWorkItem],
        updated: GhostWorkItem,
        *,
        action: str,
    ) -> GhostWorkItem:
        merged = _replace_item(items, updated)
        if not self._append_events_atomic([_item_event(updated, action)]):
            raise OSError("ghost work event write failed")
        self._write_projection_best_effort(merged)
        self._compact_if_needed(merged)
        return updated

    def _sync_failed(self, reason: str) -> GhostWorkSyncResult:
        warnings = self.last_warnings or ((reason,) if reason else ())
        self.last_warnings = _bounded_warnings(warnings)
        return GhostWorkSyncResult(False, skipped_reason=reason, warnings=self.last_warnings)

    def _load_items(self) -> tuple[GhostWorkItem, ...]:
        if self.events_path.exists():
            events = self._read_events()
            if not self._events_read_blocked:
                return tuple(_bounded_items(_items_from_events(events)))
            return self._load_projection_items()
        projection = self._load_projection_items()
        if projection:
            self.last_warnings = ("work_events_missing",)
        return projection

    def _load_items_for_event_rewrite(self) -> tuple[GhostWorkItem, ...]:
        if self.events_path.exists():
            return tuple(_bounded_items(_items_from_events(self._read_events())))
        self._events_read_blocked = False
        self.last_warnings = ()
        return self._load_projection_items()

    def _load_projection_items(self) -> tuple[GhostWorkItem, ...]:
        payload = read_json(self.projection_path, max_bytes=MAX_WORK_STATE_BYTES)
        if not isinstance(payload, dict):
            return ()
        if payload.get("schema_version") != WORK_QUEUE_SCHEMA_VERSION:
            return ()
        if payload.get("kind") != _PROJECTION_KIND:
            return ()
        return tuple(_bounded_items(
            item for item in (GhostWorkItem.from_payload(row) for row in _list(payload.get("items")))
            if item is not None
        ))

    def _read_events(self) -> list[dict[str, object]]:
        self._events_read_blocked = False
        try:
            if not self.events_path.is_file():
                self.last_warnings = ()
                return []
            if self.events_path.stat().st_size > MAX_WORK_EVENTS_BYTES:
                self.last_warnings = ("work_events_too_large",)
                self._events_read_blocked = True
                return []
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            self.last_warnings = ("work_events_unreadable",)
            self._events_read_blocked = True
            return []
        rows: list[dict[str, object]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("schema_version") == WORK_QUEUE_SCHEMA_VERSION:
                rows.append(payload)
        self.last_warnings = ()
        return rows

    def _append_events_atomic(self, events: Iterable[dict[str, object]]) -> bool:
        rows = [event for event in events if isinstance(event, dict)]
        if not rows:
            return True
        try:
            existing = []
            if self.events_path.exists():
                existing = self._read_events()
                if self._events_read_blocked:
                    return False
            elif self._load_projection_items():
                self._rewrite_events_from_items(self._load_projection_items(), control_event=None)
                existing = self._read_events()
                if self._events_read_blocked:
                    return False
            self._write_events_atomic([*existing, *rows])
            return True
        except (OSError, TypeError, ValueError):
            self.last_warnings = ("work_event_write_failed",)
            return False

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
        rows = [event for event in events if isinstance(event, dict)]
        data = "".join(_json_line(event) for event in rows).encode("utf-8")
        if len(data) > MAX_WORK_EVENTS_BYTES:
            raise ValueError("ghost work events are too large")
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.events_path.with_name(f".{self.events_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.events_path)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def _rewrite_events_from_items(
        self,
        items: Iterable[GhostWorkItem],
        *,
        control_event: dict[str, object] | None = None,
    ) -> None:
        rows = [_item_event(item, "upsert") for item in _bounded_items(items)]
        rows.append(control_event or _control_event("ghost_work_events_compacted", {"items": len(rows)}))
        self._write_events_atomic(rows)

    def _replace_items(self, items: Iterable[GhostWorkItem], reason: str) -> bool:
        rows = _bounded_items(items)
        try:
            self._rewrite_events_from_items(rows, control_event=_control_event(reason, {"items": len(rows)}))
            self._write_projection(rows, warnings=[])
            return True
        except (OSError, TypeError, ValueError):
            self.last_warnings = ("work_event_write_failed",)
            return False

    def _compact_if_needed(self, items: Iterable[GhostWorkItem]) -> None:
        stats = _event_file_stats(self.events_path, max_bytes=MAX_WORK_EVENTS_BYTES)
        if not stats["readable"]:
            self.last_warnings = (str(stats["warning"] or "work_events_unreadable"),)
            return
        if stats["events"] <= MAX_WORK_EVENTS and stats["bytes"] <= MAX_WORK_EVENTS_BYTES:
            return
        try:
            self._rewrite_events_from_items(items)
        except OSError:
            pass


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
        out.append(_new_item(
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
        ))
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
    return [_new_item(
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
    )]


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
    return [_new_item(
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
    )]


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
    return [_new_item(
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
    )]


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
) -> GhostWorkItem | None:
    project_ref = _project_ref(project)
    session_ref = _session_ref(session_id)
    candidates = [
        item for item in items
        if item.status in CLAIMABLE_STATUSES
        and item.kind in TASKRUNNER_KINDS
        and item.retry_count < MAX_WORK_RETRIES
        and _scope_matches(item, project_ref=project_ref, session_ref=session_ref)
        and mode_for_work_item(replace(item, status="running"), project=project)
    ]
    if not candidates:
        return None
    return sorted(candidates, key=_claim_sort_key)[0]


def _replace_item(items: Iterable[GhostWorkItem], updated: GhostWorkItem) -> list[GhostWorkItem]:
    by_id = {item.id: item for item in items}
    by_id[updated.id] = updated
    return _bounded_items(by_id.values())


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


def _claim_sort_key(item: GhostWorkItem) -> tuple[int, float, int, tuple[int, ...], str]:
    return (
        SCOPE_PRIORITY.get(item.scope, 99),
        -item.priority,
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
        return _scope_matches(item, project_ref=project_ref, session_ref=session_ref) if (project_ref or session_ref) else True
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
        if event.get("type") != "ghost_work_item_upsert":
            continue
        item = GhostWorkItem.from_payload(event.get("item"))
        if item is not None:
            by_id[item.id] = item
    return list(by_id.values())


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


def _item_event(item: GhostWorkItem, action: str) -> dict[str, object]:
    return {
        "schema_version": WORK_QUEUE_SCHEMA_VERSION,
        "type": "ghost_work_item_upsert",
        "event_id": "gwe_" + uuid.uuid4().hex[:24],
        "ts": _now(),
        "action": clip_signal_text(action, 40),
        "item": item.to_payload(),
    }


def _control_event(event_type: str, payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": WORK_QUEUE_SCHEMA_VERSION,
        "type": clip_signal_text(event_type, 80),
        "event_id": "gwc_" + uuid.uuid4().hex[:24],
        "ts": _now(),
        "payload": _clean_metadata(payload),
    }


def _stable_item_id(
    *,
    kind: str,
    scope: str,
    scope_ref: str,
    source: str,
    source_ref: str,
    title: str,
) -> str:
    key = "|".join((
        kind,
        scope,
        scope_ref,
        source,
        source_ref,
        " ".join(str(title or "").split()).casefold(),
    ))
    return "gwi_" + hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:24]


def _clean_item_text(value: object, *, max_chars: int) -> str:
    text = " ".join(str(value or "").replace("\r\n", "\n").replace("\r", "\n").split())
    text = clip_signal_text(text, max_chars).rstrip(".")
    if not text:
        return ""
    lower = text.casefold()
    if "ghost" in lower or "work queue" in lower or "workitem" in lower:
        return ""
    if contains_sensitive_signal_text(text) or dangerous_text(text) or not safe_rendered_body(text):
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
    return _int(receipt.get("changed_count")) > 0


def _primary_proof_matches_item_kind(item: GhostWorkItem, refs: Iterable[str]) -> bool:
    prefixes = {str(ref).split(":", 1)[0] for ref in refs}
    if item.kind in {"research", "open_question"}:
        return "research" in prefixes
    if item.kind == "review":
        return "review" in prefixes
    if item.kind in {"coding", "project_followup"}:
        return bool(prefixes.intersection({"diff", "receipt"}))
    return False


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


def _release_stale_claims(
    items: Iterable[GhostWorkItem],
    *,
    now: str,
) -> tuple[list[GhostWorkItem], list[GhostWorkItem]]:
    updated: list[GhostWorkItem] = []
    stale: list[GhostWorkItem] = []
    for item in items:
        if _is_stale_claim(item, now):
            next_status = "blocked" if item.retry_count >= MAX_WORK_RETRIES else "queued"
            released = replace(
                item,
                status=next_status,
                started_run_id="" if next_status == "queued" else item.started_run_id,
                lease_expires_at="",
                blocked_reason="stale_claim" if next_status == "blocked" else "",
                updated_at=now,
            )
            updated.append(released)
            stale.append(released)
            continue
        updated.append(item)
    return _bounded_items(updated), stale


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
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, number)), 4)


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _list(value: object) -> list[object]:
    if isinstance(value, tuple):
        return list(value)
    return value if isinstance(value, list) else []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _future_ts(now: str, seconds: int) -> str:
    base = _parse_ts(now)
    try:
        delta_seconds = int(seconds)
    except (TypeError, ValueError):
        delta_seconds = DEFAULT_WORK_CLAIM_LEASE_SECONDS
    return datetime.fromtimestamp(
        base.timestamp() + delta_seconds,
        tz=timezone.utc,
    ).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_line(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"


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


def _event_file_stats(path: Path, *, max_bytes: int) -> dict[str, object]:
    try:
        if not path.is_file():
            return {"events": 0, "bytes": 0, "readable": True, "warning": ""}
        event_bytes = path.stat().st_size
        if event_bytes > max(0, int(max_bytes or 0)):
            return {
                "events": 0,
                "bytes": event_bytes,
                "readable": True,
                "warning": "work_events_too_large",
            }
        event_count = len(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return {"events": 0, "bytes": 0, "readable": False, "warning": "work_events_unreadable"}
    return {"events": event_count, "bytes": event_bytes, "readable": True, "warning": ""}


def _compact_payload(
    ok: bool,
    compacted: bool,
    before: Mapping[str, object],
    after: Mapping[str, object],
    warnings: Iterable[object],
) -> dict[str, object]:
    return {
        "ok": bool(ok),
        "compacted": bool(compacted),
        "events_before": _int(before.get("events")),
        "events_after": _int(after.get("events")),
        "bytes_before": _int(before.get("bytes")),
        "bytes_after": _int(after.get("bytes")),
        "warnings": list(_bounded_warnings(warnings)),
    }


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
