"""Bounded local work-item queue for Ghost continuity.

The queue is a local state machine, not an autonomous runner. It can remember
audited follow-up work and claim one item when the user explicitly asks to
continue. Task entry runs the claimed item; Ghost post-turn policy updates the
queue from terminal task facts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, is_dataclass, replace
from pathlib import Path
from typing import Any

from codey.ghost import _common
from codey.ghost.affinity import apply_affinity_work_boost
from codey.ghost.continuity import GhostContinuityStore
from codey.ghost.event_log import (
    GhostEventLog,
)
from codey.ghost.event_log import (
    compact_result_payload as _compact_payload,
)
from codey.ghost.event_log import (
    event_file_stats as _event_file_stats,
)
from codey.ghost.schema import clip_signal_text
from codey.ghost.work_queue_events import (
    _PROJECTION_KIND,
    _WORK_EVENT_TYPES,
    _apply_items_deleted_event,
    _apply_transition_event,
    _bounded_items,
    _bounded_warnings,
    _event_read_warnings,
    _item_payloads,
    _item_sort_key,
    _items_deleted_event,
    _items_from_events,
    _merge_items,
    _observed_event,
    _primary_proof_matches_item_kind,
    _projection_payload,
    _receipt_proves_project_work,
    _scope_filter_matches,
    _scope_matches,
    _snapshot_event,
    _snapshot_items,
    _transition_allowed,
    _transition_event,
    _valid_work_event,
)
from codey.ghost.work_queue_events import (
    _WORK_TRANSITION_PATCH_KEYS as _WORK_TRANSITION_PATCH_KEYS,
)
from codey.ghost.work_queue_events import (
    WORK_ITEM_TRANSITION_ACTIONS as WORK_ITEM_TRANSITION_ACTIONS,
)
from codey.ghost.work_queue_events import (
    WORK_ITEM_TRANSITION_MATRIX as WORK_ITEM_TRANSITION_MATRIX,
)
from codey.ghost.work_queue_events import (
    _apply_block_transition as _apply_block_transition,
)
from codey.ghost.work_queue_events import (
    _apply_claim_transition as _apply_claim_transition,
)
from codey.ghost.work_queue_events import (
    _apply_queue_transition as _apply_queue_transition,
)
from codey.ghost.work_queue_events import (
    _apply_reject_transition as _apply_reject_transition,
)
from codey.ghost.work_queue_events import (
    _valid_claim_transition as _valid_claim_transition,
)
from codey.ghost.work_queue_events import (
    _valid_release_transition as _valid_release_transition,
)
from codey.ghost.work_queue_events import (
    _valid_work_transition as _valid_work_transition,
)
from codey.ghost.work_queue_model import (
    CLAIMABLE_STATUSES,
    DEFAULT_WORK_CLAIM_LEASE_SECONDS,
    MAX_WORK_EVENTS,
    MAX_WORK_EVENTS_BYTES,
    MAX_WORK_REF_CHARS,
    MAX_WORK_RETRIES,
    MAX_WORK_STATE_BYTES,
    MAX_WORK_TITLE_CHARS,
    MAX_WORK_WARNINGS,
    MAX_WORK_WHY_CHARS,
    SCOPE_PRIORITY,
    TASKRUNNER_KINDS,
    WORK_ITEM_KINDS,
    WORK_ITEM_STATUSES,
    WORK_QUEUE_SCHEMA_VERSION,
    GhostWorkClaimResult,
    GhostWorkItem,
    GhostWorkSyncResult,
    _bounded_refs,
    _clean_item_text,
    _clean_scope,
    _clean_status,
    _future_ts,
    _int,
    _is_expired,
    _is_stale_claim,
    _list,
    _project_ref,
    _proof_run_ref,
    _session_ref,
)
from codey.ghost.work_queue_model import (
    _research_proof_ref as _research_proof_ref,
)
from codey.ghost.work_queue_sources import (
    _items_from_continuity,
    _items_from_research_interest_candidates,
    _items_from_run_projection,
    _items_from_terminal_event,
    _items_from_work_checkpoint,
)
from codey.ghost.work_queue_sources import (
    _new_item as _new_item,
)
from codey.storage.event_state import reset_event_backed_state
from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import (
    DEFAULT_STATE_HOME,
    StoreCorruption,
    backup_corrupt_file,
    read_json_strict,
    write_json_atomic,
)

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
            now = _common.now_iso_z()
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
        statuses = _common.filter_values(status, WORK_ITEM_STATUSES)
        kinds = _common.filter_values(kind, WORK_ITEM_KINDS)
        project_ref = _project_ref(project)
        session_ref = _session_ref(session_id)
        rows = []
        now = _common.now_iso_z()
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
                now = _common.now_iso_z()
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
                now = _common.now_iso_z()
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
                raise OSError("ghost work events are unreadable") from None
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
                now = _common.now_iso_z()
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
                raise OSError("ghost work events are unreadable") from None
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
                now = _common.now_iso_z()
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
                raise OSError("ghost work events are unreadable") from None
            raise

    def reconcile_stale_claims(self) -> GhostWorkSyncResult:
        try:

            def decide(events: list[dict[str, object]]) -> _WorkMutation:
                now = _common.now_iso_z()
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
            projection = _projection_payload(items, generated_at=_common.now_iso_z(), warnings=event_warnings)
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
                    ts=_common.now_iso_z(),
                )
                new_items = _bounded_items(_items_from_events((*events, event)))
                return _WorkMutation(
                    {"removed": len(removed), "warnings": []}, append_events=(event,), items=tuple(new_items)
                )

            result = self._mutate_event_log(decide)
            return result if isinstance(result, dict) else {"removed": 0, "warnings": ["work_queue_error"]}
        except (OSError, TypeError, ValueError):
            if self._events_read_blocked:
                raise OSError("ghost work events are unreadable") from None
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
                self._write_events_atomic([_snapshot_event(items, ts=_common.now_iso_z(), reason="events_compacted")])
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
                now = _common.now_iso_z()
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
                raise OSError("ghost work events are unreadable") from None
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
        try:
            payload = read_json_strict(self.projection_path, max_bytes=MAX_WORK_STATE_BYTES)
        except StoreCorruption:
            backup_corrupt_file(self.projection_path)
            return ()
        if not isinstance(payload, dict):
            return ()
        if type(payload.get("schema_version")) is not int or payload.get("schema_version") != WORK_QUEUE_SCHEMA_VERSION:
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
            now = _common.event_ts(event)
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
            raise OSError("ghost work events are unreadable") from None
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
            _projection_payload(items, generated_at=_common.now_iso_z(), warnings=warnings),
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
            self._write_events_atomic([_snapshot_event(items, ts=_common.now_iso_z(), reason="events_compacted")])
        except (OSError, TypeError, ValueError):
            self.last_warnings = _bounded_warnings((*self.last_warnings, "work_compaction_failed"))


def is_strict_work_continuation(value: object) -> bool:
    normalized = _normalize_continuation_text(value)
    if not normalized:
        return False
    return bool(normalized in _STRICT_CONTINUATION_CN or normalized in _STRICT_CONTINUATION_EN)


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
    return _common.find_work_item_by_id(items, item_id)


def _claim_sort_key(
    item: GhostWorkItem, affinity_hints: Iterable[Any] = ()
) -> tuple[int, float, int, tuple[int, ...], str]:
    priority = apply_affinity_work_boost(item.priority, affinity_hints, item.id)
    return (
        SCOPE_PRIORITY.get(item.scope, 99),
        -priority,
        item.retry_count,
        _common.reverse_text_sort_key(item.updated_at),
        item.id,
    )


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
