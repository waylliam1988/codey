"""Auditable Ghost memory inbox projection and event log."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import SupportsIndex, SupportsInt, TypeAlias, cast

from codey.ghost import _common
from codey.ghost.events import (
    control_event as _ghost_control_event,
)
from codey.ghost.gate import (
    CANDIDATE_STATUSES,
    CANDIDATE_TYPES,
    GhostGateDecision,
    GhostMemoryGate,
    candidate_type_for_signal_kind,
)
from codey.ghost.numbers import coerce_unit_float
from codey.ghost.record_store import GhostRecords
from codey.ghost.schema import (
    SCHEMA_VERSION as SIGNAL_SCHEMA_VERSION,
)
from codey.ghost.schema import (
    SIGNAL_KINDS,
    SIGNAL_SCOPES,
    GhostSignal,
    clip_signal_text,
)
from codey.ghost.sqlite import GhostSQLite
from codey.ghost.typed_fields import metadata_conflict_key, metadata_value_key
from codey.runtime.core import cancellation
from codey.storage.local_store import (
    DEFAULT_STATE_HOME,
)

_INT_INPUT: TypeAlias = str | bytes | bytearray | SupportsInt | SupportsIndex

INBOX_SCHEMA_VERSION = 1
MAX_GHOST_EVENTS = 5_000
MAX_INBOX_ITEMS = 200
MAX_EVENTS_BYTES = 4 * 1024 * 1024
MAX_CANDIDATE_METADATA_KEYS = 8
MAX_CANDIDATE_EVIDENCE_REFS = 32

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.IGNORECASE)
_PROJECTION_KIND = "ghost_memory_inbox_projection"
_STORED_REJECTION_REASONS = {"confidence_below_candidate_threshold"}


class GhostSettingsError(ValueError):
    """Persisted learning settings are invalid; callers must fail closed."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"corrupt ghost settings {path}: {reason}")
        self.path = Path(path)
        self.reason = reason


@dataclass(frozen=True)
class GhostMemoryCandidate:
    id: str
    candidate_type: str
    signal_kind: str
    status: str
    scope: str
    summary: str
    evidence_quote: str
    confidence: float
    conflict_key: str
    value_key: str
    session_id: str
    run_id: str
    project: str
    created_at: str
    updated_at: str
    gate_reason: str
    metadata: dict[str, object] = field(default_factory=dict)
    reinforcement_count: int = 1
    evidence_refs: tuple[str, ...] = ()
    reviewed_at: str = ""
    reviewed_by: str = ""
    superseded_by: str = ""

    @property
    def scope_ref(self) -> str:
        if self.scope == "project":
            return self.project
        if self.scope == "session":
            return self.session_id
        return ""

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "candidate_type": self.candidate_type,
            "signal_kind": self.signal_kind,
            "status": self.status,
            "scope": self.scope,
            "summary": self.summary,
            "evidence_quote": self.evidence_quote,
            "confidence": self.confidence,
            "conflict_key": self.conflict_key,
            "value_key": self.value_key,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "project": self.project,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "gate_reason": self.gate_reason,
            "metadata": dict(self.metadata),
            "reinforcement_count": self.reinforcement_count,
            "evidence_refs": list(self.evidence_refs),
            "reviewed_at": self.reviewed_at,
            "reviewed_by": self.reviewed_by,
            "superseded_by": self.superseded_by,
        }

    @classmethod
    def from_payload(cls, payload: object) -> GhostMemoryCandidate | None:
        if not isinstance(payload, dict):
            return None
        candidate_type = str(payload.get("candidate_type") or "").strip()
        signal_kind = str(payload.get("signal_kind") or "").strip().lower()
        status = str(payload.get("status") or "").strip().lower()
        scope = str(payload.get("scope") or "").strip().lower()
        if candidate_type not in CANDIDATE_TYPES:
            return None
        if signal_kind not in SIGNAL_KINDS:
            return None
        if status not in CANDIDATE_STATUSES:
            return None
        if scope not in SIGNAL_SCOPES:
            return None
        candidate_id = clip_signal_text(payload.get("id"), 120)
        summary = clip_signal_text(payload.get("summary"))
        evidence_quote = clip_signal_text(payload.get("evidence_quote"), 240)
        conflict_key = clip_signal_text(payload.get("conflict_key"), 180)
        if not candidate_id or not summary or not evidence_quote or not conflict_key:
            return None
        confidence = _coerce_confidence(payload.get("confidence"))
        if confidence is None:
            return None
        reinforcement_count = max(1, _int_or_default(payload.get("reinforcement_count"), 1))
        value_key = clip_signal_text(payload.get("value_key"), 180)
        if not value_key:
            return None
        return cls(
            id=candidate_id,
            candidate_type=candidate_type,
            signal_kind=signal_kind,
            status=status,
            scope=scope,
            summary=summary,
            evidence_quote=evidence_quote,
            confidence=confidence,
            conflict_key=conflict_key,
            value_key=value_key,
            session_id=clip_signal_text(payload.get("session_id"), 120),
            run_id=clip_signal_text(payload.get("run_id"), 120),
            project=_common.normalize_project(payload.get("project")),
            created_at=clip_signal_text(payload.get("created_at"), 80),
            updated_at=clip_signal_text(payload.get("updated_at"), 80),
            gate_reason=clip_signal_text(payload.get("gate_reason"), 160),
            metadata=_clean_metadata(payload.get("metadata")),
            reinforcement_count=reinforcement_count,
            evidence_refs=_clean_evidence_refs(
                payload.get("evidence_refs"),
                candidate_id=candidate_id,
                reinforcement_count=reinforcement_count,
            ),
            reviewed_at=clip_signal_text(payload.get("reviewed_at"), 80),
            reviewed_by=clip_signal_text(payload.get("reviewed_by"), 80),
            superseded_by=clip_signal_text(payload.get("superseded_by"), 120),
        )


class GhostInboxStore:
    def __init__(
        self,
        state_home: str | Path = DEFAULT_STATE_HOME,
        *,
        gate: GhostMemoryGate | None = None,
    ) -> None:
        self.gate = gate or GhostMemoryGate()
        self.database = GhostSQLite(state_home)
        self.records = GhostRecords(self.database, "inbox")
        self.last_warnings: tuple[str, ...] = ()

    def ingest_signals(
        self,
        signals: Iterable[GhostSignal],
        *,
        session_id: str = "",
        run_id: str = "",
        project: str = "",
        user_text: str = "",
    ) -> tuple[GhostMemoryCandidate, ...]:
        signals = tuple(signals)
        if not signals:
            return ()
        try:
            with self.records.transaction():
                if not self.learning_enabled():
                    return ()
                loaded = self._load_candidates_unlocked()
                candidates = list(loaded)
                changed: list[GhostMemoryCandidate] = []
                events: list[dict[str, object]] = []
                for signal in signals:
                    decision = self.gate.evaluate(
                        signal,
                        session_id=session_id,
                        run_id=run_id,
                        project=project,
                        user_text=user_text,
                    )
                    if not _should_store_decision(decision):
                        events.append(self._sanitized_rejection_event(signal, decision))
                        continue
                    candidate = self._candidate_from_signal(
                        signal,
                        decision=decision,
                        session_id=session_id,
                        run_id=run_id,
                        project=project,
                    )
                    existing_index = self._find_conflict_index(
                        candidates,
                        candidate,
                        manual_proposal=signal.source == "manual",
                    )
                    action = "created"
                    if existing_index is None:
                        candidates.append(candidate)
                    else:
                        action = "updated"
                        candidate = self._merge_candidate(candidates[existing_index], candidate)
                        candidates[existing_index] = candidate
                    changed.append(candidate)
                    events.append(self._candidate_event(candidate, action=action))
                if not events:
                    return ()
                candidates = self._bounded_candidates(candidates)
                self._append_events(events)
                self._write_state(candidates)
                self._compact_if_needed(candidates)
                return tuple(changed)
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            return ()

    def list_candidates(
        self,
        *,
        status: str | Iterable[str] | None = None,
        scope: str = "",
        project: str = "",
        session_id: str = "",
    ) -> tuple[GhostMemoryCandidate, ...]:
        with self.records.transaction(write=False):
            return self._list_candidates_unlocked(
                status=status,
                scope=scope,
                project=project,
                session_id=session_id,
            )

    def _list_candidates_unlocked(
        self,
        *,
        status: str | Iterable[str] | None = None,
        scope: str = "",
        project: str = "",
        session_id: str = "",
    ) -> tuple[GhostMemoryCandidate, ...]:
        try:
            rows = self._filter_candidates(
                self._load_candidates_unlocked(),
                status=status,
                scope=scope,
                project=project,
                session_id=session_id,
                applicable=False,
            )
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            return ()
        return tuple(rows)

    def applicable_candidates(
        self,
        *,
        project: str = "",
        session_id: str = "",
        status: str | Iterable[str] | None = None,
    ) -> tuple[GhostMemoryCandidate, ...]:
        with self.records.transaction(write=False):
            return self._applicable_candidates_unlocked(
                project=project,
                session_id=session_id,
                status=status,
            )

    def _applicable_candidates_unlocked(
        self,
        *,
        project: str = "",
        session_id: str = "",
        status: str | Iterable[str] | None = None,
    ) -> tuple[GhostMemoryCandidate, ...]:
        try:
            rows = self._filter_candidates(
                self._load_candidates_unlocked(),
                status=status,
                scope="",
                project=project,
                session_id=session_id,
                applicable=True,
            )
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except Exception:
            return ()
        priority = {"session": 0, "project": 1, "user": 2}
        rows.sort(key=lambda item: item.updated_at, reverse=True)
        rows.sort(key=lambda item: priority[item.scope])
        return tuple(rows)

    def export_state(self) -> dict[str, object]:
        with self.records.transaction(write=False):
            candidates = [candidate.to_payload() for candidate in self._load_candidates_unlocked()]
            events = [event for event in self._read_events_unlocked()]
            return {
                "schema_version": INBOX_SCHEMA_VERSION,
                "settings": self._read_settings_unlocked(),
                "inbox": self._projection_payload(candidates),
                "events": events,
                "warnings": list(self.last_warnings),
            }

    def review_candidate(
        self,
        candidate_id: str,
        action: str,
        *,
        reviewed_by: str = "cli",
    ) -> GhostMemoryCandidate | None:
        normalized_id = clip_signal_text(candidate_id, 120)
        normalized_action = (action or "").strip().lower()
        if normalized_action not in {"accept", "reject"}:
            raise ValueError("action must be accept or reject")
        if not normalized_id:
            return None
        with self.records.transaction():
            candidates = list(self._load_candidates_unlocked())
            target_index: int | None = None
            for index, candidate in enumerate(candidates):
                if candidate.id == normalized_id:
                    target_index = index
                    break
            if target_index is None:
                return None
            now = _common.now_iso_z()
            reviewer = clip_signal_text(reviewed_by or "cli", 80)
            target = candidates[target_index]
            new_status = "accepted" if normalized_action == "accept" else "rejected"
            target = replace(
                target,
                status=new_status,
                updated_at=now,
                reviewed_at=now,
                reviewed_by=reviewer,
                gate_reason=f"manual_{normalized_action}",
                superseded_by="" if new_status == "accepted" else target.superseded_by,
            )
            candidates[target_index] = target
            changed: list[tuple[GhostMemoryCandidate, str]] = [(target, f"review_{normalized_action}")]
            superseded_ids: list[str] = []
            if new_status == "accepted":
                target_ref = _scope_ref(target)
                for index, candidate in enumerate(candidates):
                    if candidate.id == target.id:
                        continue
                    if candidate.status != "accepted":
                        continue
                    if candidate.scope != target.scope or _scope_ref(candidate) != target_ref:
                        continue
                    if candidate.conflict_key != target.conflict_key:
                        continue
                    if candidate.value_key == target.value_key:
                        continue
                    superseded = replace(
                        candidate,
                        status="superseded",
                        updated_at=now,
                        reviewed_at=now,
                        reviewed_by=reviewer,
                        superseded_by=target.id,
                    )
                    candidates[index] = superseded
                    changed.append((superseded, "superseded"))
                    superseded_ids.append(superseded.id)
            events = [self._candidate_event(candidate, action=event_action) for candidate, event_action in changed]
            events.append(
                _ghost_control_event(
                    schema_version=INBOX_SCHEMA_VERSION,
                    event_name="ghost_memory_candidate_reviewed",
                    payload={
                        "candidate_id": target.id,
                        "action": normalized_action,
                        "reviewed_by": reviewer,
                        "superseded_ids": superseded_ids,
                    },
                    now=_common.now_iso_z(),
                )
            )
            self._append_events(events)
            self._write_state(candidates)
            self._compact_if_needed(candidates)
            return target

    def reset_all(self, *, preserve_settings: bool = True) -> bool:
        self.records.reset(settings=not preserve_settings)
        self.last_warnings = ()
        return True

    def delete_scope(
        self,
        scope: str,
        *,
        project: str = "",
        session_id: str = "",
    ) -> int:
        normalized_scope = (scope or "").strip().lower()
        if normalized_scope not in SIGNAL_SCOPES:
            raise ValueError("scope must be user, project, or session")
        normalized_project = _common.normalize_project(project)
        normalized_session = clip_signal_text(session_id, 120)
        if normalized_scope == "project" and not normalized_project:
            raise ValueError("project is required for project scope deletion")
        if normalized_scope == "session" and not normalized_session:
            raise ValueError("session_id is required for session scope deletion")
        with self.records.transaction():
            candidates = list(self._load_candidates_unlocked())
            remaining: list[GhostMemoryCandidate] = []
            removed = 0
            for candidate in candidates:
                if _scope_delete_match(
                    candidate,
                    normalized_scope,
                    project=normalized_project,
                    session_id=normalized_session,
                ):
                    removed += 1
                else:
                    remaining.append(candidate)
            if not removed:
                return 0
            control = _ghost_control_event(
                schema_version=INBOX_SCHEMA_VERSION,
                event_name="ghost_memory_scope_deleted",
                payload={
                    "scope": normalized_scope,
                    "project": normalized_project if normalized_scope == "project" else "",
                    "session_id": normalized_session if normalized_scope == "session" else "",
                    "removed_count": removed,
                },
                now=_common.now_iso_z(),
            )
            self._rewrite_events_from_candidates(remaining, control_event=control)
            self._write_state(remaining)
            return removed

    def set_learning_enabled(self, enabled: bool) -> bool:
        if type(enabled) is not bool:
            raise ValueError("learning_enabled must be bool")
        payload = {
            "schema_version": INBOX_SCHEMA_VERSION,
            "learning_enabled": enabled,
            "updated_at": _common.now_iso_z(),
        }
        try:
            with self.records.transaction():
                self.records.set_setting("learning", payload)
                self._append_events(
                    [
                        _ghost_control_event(
                            schema_version=INBOX_SCHEMA_VERSION,
                            event_name="ghost_learning_settings_updated",
                            payload={"learning_enabled": enabled},
                            now=_common.now_iso_z(),
                        )
                    ]
                )
                self.records.mark_applied()
        except (OSError, TypeError, ValueError):
            return False
        return True

    def learning_enabled(self) -> bool:
        """Read the SQLite switch; only an absent setting defaults to enabled."""
        with self.records.transaction(write=False):
            return bool(self._read_settings_unlocked()["learning_enabled"])

    def compact_if_needed(self) -> dict[str, object]:
        with self.records.transaction():
            before = self.records.event_stats()
            items = self._load_candidates_unlocked()
            self._compact_if_needed(items)
            after = self.records.event_stats()
            return {
                "ok": True,
                "compacted": after != before,
                "events_before": before["events"],
                "events_after": after["events"],
                "bytes_before": before["bytes"],
                "bytes_after": after["bytes"],
                "warnings": list(self.last_warnings),
            }

    def _candidate_from_signal(
        self,
        signal: GhostSignal,
        *,
        decision: GhostGateDecision,
        session_id: str,
        run_id: str,
        project: str,
    ) -> GhostMemoryCandidate:
        now = _common.now_iso_z()
        signal_kind = (signal.kind or "").strip().lower()
        candidate_id = "gmc_" + uuid.uuid4().hex[:24]
        return GhostMemoryCandidate(
            id=candidate_id,
            candidate_type=decision.candidate_type or candidate_type_for_signal_kind(signal_kind),
            signal_kind=signal_kind,
            status=decision.status,
            scope=(signal.scope or "").strip().lower(),
            summary=clip_signal_text(signal.summary),
            evidence_quote=clip_signal_text(signal.evidence_quote, 240),
            confidence=_coerce_confidence(signal.confidence) or 0.0,
            conflict_key=conflict_key_for_signal(signal),
            value_key=value_key_for_signal(signal),
            session_id=clip_signal_text(session_id, 120),
            run_id=clip_signal_text(run_id, 120),
            project=_common.normalize_project(project),
            created_at=now,
            updated_at=now,
            gate_reason=decision.reason,
            metadata=_clean_metadata(signal.metadata),
            reinforcement_count=1,
            evidence_refs=(f"{candidate_id}:1",),
        )

    def _merge_candidate(
        self,
        current: GhostMemoryCandidate,
        incoming: GhostMemoryCandidate,
    ) -> GhostMemoryCandidate:
        reinforcement_count = current.reinforcement_count + 1
        reviewed = bool(current.reviewed_at)
        return replace(
            current,
            status=_merged_ingest_status(
                current.status,
                incoming.status,
                reviewed=reviewed,
            ),
            summary=incoming.summary or current.summary,
            evidence_quote=incoming.evidence_quote or current.evidence_quote,
            confidence=max(current.confidence, incoming.confidence),
            session_id=incoming.session_id or current.session_id,
            run_id=incoming.run_id or current.run_id,
            project=incoming.project or current.project,
            updated_at=incoming.updated_at,
            gate_reason=current.gate_reason if reviewed else incoming.gate_reason,
            metadata=_merge_metadata(current.metadata, incoming.metadata),
            reinforcement_count=reinforcement_count,
            evidence_refs=_append_evidence_ref(current, reinforcement_count),
        )

    def _find_conflict_index(
        self,
        candidates: list[GhostMemoryCandidate],
        incoming: GhostMemoryCandidate,
        *,
        manual_proposal: bool = False,
    ) -> int | None:
        incoming_ref = _scope_ref(incoming)
        for index, candidate in enumerate(candidates):
            if manual_proposal and candidate.status in {"rejected", "superseded"}:
                continue
            if candidate.scope != incoming.scope:
                continue
            if candidate.conflict_key != incoming.conflict_key:
                continue
            if candidate.value_key != incoming.value_key:
                continue
            if _scope_ref(candidate) != incoming_ref:
                continue
            return index
        return None

    def _filter_candidates(
        self,
        candidates: Iterable[GhostMemoryCandidate],
        *,
        status: str | Iterable[str] | None,
        scope: str,
        project: str,
        session_id: str,
        applicable: bool,
    ) -> list[GhostMemoryCandidate]:
        statuses = _status_filter(status)
        normalized_scope = (scope or "").strip().lower()
        normalized_project = _common.normalize_project(project)
        normalized_session = clip_signal_text(session_id, 120)
        rows: list[GhostMemoryCandidate] = []
        for candidate in candidates:
            if statuses and candidate.status not in statuses:
                continue
            if normalized_scope and candidate.scope != normalized_scope:
                continue
            if applicable:
                if not _scope_applies(candidate, project=normalized_project, session_id=normalized_session):
                    continue
            elif not _scope_filter_matches(
                candidate,
                project=normalized_project,
                session_id=normalized_session,
            ):
                continue
            rows.append(candidate)
        return sorted(rows, key=lambda item: item.updated_at, reverse=True)

    def _load_candidates_unlocked(self) -> tuple[GhostMemoryCandidate, ...]:
        rows = []
        for payload in self.records.rows():
            candidate = GhostMemoryCandidate.from_payload(payload)
            if candidate is None:
                raise ValueError("invalid stored Ghost candidate")
            rows.append(candidate)
        return tuple(self._bounded_candidates(rows))

    def _rebuild_candidates_from_events_unlocked(self) -> list[GhostMemoryCandidate] | None:
        by_id: dict[str, GhostMemoryCandidate] = {}
        events = self._read_events_unlocked()
        for event in events:
            event_type = str(event.get("type") or "")
            if event_type != "ghost_memory_candidate_upsert":
                continue
            candidate = GhostMemoryCandidate.from_payload(event.get("candidate"))
            if candidate is not None:
                by_id[candidate.id] = candidate
        return self._bounded_candidates(by_id.values())

    def _read_events_unlocked(self) -> list[dict[str, object]]:
        return self.records.events()

    def _read_settings_unlocked(self) -> dict[str, object]:
        payload = self.records.setting("learning", {"schema_version": INBOX_SCHEMA_VERSION, "learning_enabled": True})
        if not isinstance(payload, dict) or type(payload.get("learning_enabled")) is not bool:
            raise GhostSettingsError(self.database.path, "learning_enabled must be bool")
        if type(payload.get("schema_version")) is not int or payload["schema_version"] != INBOX_SCHEMA_VERSION:
            raise GhostSettingsError(self.database.path, "schema mismatch")
        return payload

    def _write_state(self, candidates: Iterable[GhostMemoryCandidate]) -> None:
        self.records.replace_rows(
            dict(candidate.to_payload(), scope_ref=candidate.scope_ref)
            for candidate in self._bounded_candidates(candidates)
        )

    def _projection_payload(self, candidates: list[dict[str, object]]) -> dict[str, object]:
        return {
            "schema_version": INBOX_SCHEMA_VERSION,
            "kind": _PROJECTION_KIND,
            "source": "ghost.sqlite3:inbox",
            "updated_at": _common.now_iso_z(),
            "candidates": candidates[:MAX_INBOX_ITEMS],
            "warnings": list(self.last_warnings),
        }

    def _append_events(self, events: Iterable[dict[str, object]]) -> None:
        self.records.append(events)

    def _rewrite_events_from_candidates(
        self,
        candidates: Iterable[GhostMemoryCandidate],
        *,
        control_event: dict[str, object] | None = None,
    ) -> None:
        events: list[dict[str, object]] = [
            self._candidate_event(candidate, action="compacted") for candidate in self._bounded_candidates(candidates)
        ]
        if control_event is not None:
            events.append(control_event)
        self._write_events_atomic(events)

    def _write_events_atomic(self, events: Iterable[dict[str, object]]) -> None:
        self.records.append(events, replace=True)

    def _compact_if_needed(self, candidates: Iterable[GhostMemoryCandidate]) -> None:
        stats = self.records.event_stats()
        if stats["events"] > MAX_GHOST_EVENTS or stats["bytes"] > MAX_EVENTS_BYTES:
            self._rewrite_events_from_candidates(candidates, control_event=self._compacted_event("event_limit"))
            self.records.mark_applied()

    def _compacted_event(self, reason: str) -> dict[str, object]:
        return _ghost_control_event(
            schema_version=INBOX_SCHEMA_VERSION,
            event_name="ghost_memory_store_compacted",
            payload={
                "reason": clip_signal_text(reason, 80),
                "max_events": MAX_GHOST_EVENTS,
                "max_event_bytes": MAX_EVENTS_BYTES,
            },
            now=_common.now_iso_z(),
        )

    def _candidate_event(
        self,
        candidate: GhostMemoryCandidate,
        *,
        action: str,
    ) -> dict[str, object]:
        return {
            "schema_version": INBOX_SCHEMA_VERSION,
            "type": "ghost_memory_candidate_upsert",
            "ts": _common.now_iso_z(),
            "action": action,
            "signal_schema_version": SIGNAL_SCHEMA_VERSION,
            "candidate": candidate.to_payload(),
        }

    def _sanitized_rejection_event(
        self,
        signal: GhostSignal,
        decision: GhostGateDecision,
    ) -> dict[str, object]:
        return {
            "schema_version": INBOX_SCHEMA_VERSION,
            "type": "ghost_memory_candidate_rejected",
            "ts": _common.now_iso_z(),
            "signal_schema_version": SIGNAL_SCHEMA_VERSION,
            "signal_kind": clip_signal_text(getattr(signal, "kind", ""), 80),
            "scope": clip_signal_text(getattr(signal, "scope", ""), 40),
            "gate": decision.to_payload(),
            "stored_candidate": False,
        }

    def _bounded_candidates(
        self,
        candidates: Iterable[GhostMemoryCandidate],
    ) -> list[GhostMemoryCandidate]:
        rows = list(candidates)
        rows.sort(key=lambda item: item.updated_at or item.created_at, reverse=True)
        return rows[:MAX_INBOX_ITEMS]


def conflict_key_for_signal(signal: GhostSignal) -> str:
    kind = (signal.kind or "").strip().lower()
    metadata_key = _metadata_conflict_key(getattr(signal, "metadata", {}) or {})
    if metadata_key:
        return f"{kind}:{metadata_key}"
    text = f"{signal.summary} {signal.evidence_quote}".casefold()
    return f"{kind}:{_token_slug(text)}"


def value_key_for_signal(signal: GhostSignal) -> str:
    metadata_key = _metadata_value_key(getattr(signal, "metadata", {}) or {})
    if metadata_key:
        return metadata_key
    return _derived_value_key(signal.summary, signal.evidence_quote)


def _metadata_conflict_key(metadata: object) -> str:
    return metadata_conflict_key(metadata)


def _metadata_value_key(metadata: object) -> str:
    return metadata_value_key(metadata)


def _derived_value_key(summary: object, evidence_quote: object) -> str:
    text = f"{summary} {evidence_quote}".casefold()
    return _token_slug(text)


def _token_slug(value: object) -> str:
    tokens: list[str] = [token.casefold() for token in _TOKEN_RE.findall(str(value or ""))]
    if not tokens:
        return "empty"
    return "_".join(tokens[:8])[:120]


def _scope_ref(candidate: GhostMemoryCandidate) -> str:
    return candidate.scope_ref


def _scope_filter_matches(
    candidate: GhostMemoryCandidate,
    *,
    project: str,
    session_id: str,
) -> bool:
    if project and candidate.scope == "project" and candidate.project != project:
        return False
    return not (session_id and candidate.scope == "session" and candidate.session_id != session_id)


def _scope_applies(
    candidate: GhostMemoryCandidate,
    *,
    project: str,
    session_id: str,
) -> bool:
    if candidate.scope == "user":
        return True
    if candidate.scope == "project":
        return bool(project) and candidate.project == project
    if candidate.scope == "session":
        return bool(session_id) and candidate.session_id == session_id
    return False


def _scope_delete_match(
    candidate: GhostMemoryCandidate,
    scope: str,
    *,
    project: str,
    session_id: str,
) -> bool:
    if candidate.scope != scope:
        return False
    if scope == "project":
        return candidate.project == project
    if scope == "session":
        return candidate.session_id == session_id
    return True


def _status_filter(value: str | Iterable[str] | None) -> set[str]:
    if value is None:
        return set()
    raw_values = value.split(",") if isinstance(value, str) else list(value)
    return {item.strip().lower() for item in raw_values if item.strip().lower() in CANDIDATE_STATUSES}


def _should_store_decision(decision: GhostGateDecision) -> bool:
    if decision.status != "rejected":
        return True
    return decision.reason in _STORED_REJECTION_REASONS


def _merged_ingest_status(
    current_status: str,
    incoming_status: str,
    *,
    reviewed: bool = False,
) -> str:
    if reviewed and current_status in {"accepted", "rejected", "superseded"}:
        return current_status
    if current_status == "superseded":
        return "superseded"
    if current_status in {"accepted", "superseded"} and incoming_status in {"candidate", "rejected"}:
        return current_status
    if incoming_status == "accepted":
        return "accepted"
    if current_status == "accepted":
        return "accepted"
    return incoming_status or current_status


def _append_evidence_ref(
    candidate: GhostMemoryCandidate,
    reinforcement_count: int,
) -> tuple[str, ...]:
    refs = list(candidate.evidence_refs)
    ref = f"{candidate.id}:{max(1, reinforcement_count)}"
    if ref not in refs:
        refs.append(ref)
    return tuple(refs[-MAX_CANDIDATE_EVIDENCE_REFS:])


def _clean_evidence_refs(
    value: object,
    *,
    candidate_id: str,
    reinforcement_count: int,
) -> tuple[str, ...]:
    refs: list[str] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            ref = clip_signal_text(item, 160)
            if ref and ref not in refs:
                refs.append(ref)
            if len(refs) >= MAX_CANDIDATE_EVIDENCE_REFS:
                break
    if refs:
        return tuple(refs)
    count = min(MAX_CANDIDATE_EVIDENCE_REFS, max(1, reinforcement_count))
    return tuple(f"{candidate_id}:{index}" for index in range(1, count + 1))


def _clean_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    clean: dict[str, object] = {}
    for key, item in value.items():
        text_key = clip_signal_text(key, 80)
        if not text_key or text_key in {"user_text", "evidence_source_text"}:
            continue
        if isinstance(item, str):
            clean[text_key] = clip_signal_text(item, 160)
        elif isinstance(item, (int, float, bool)) or item is None:
            clean[text_key] = item
        if len(clean) >= MAX_CANDIDATE_METADATA_KEYS:
            break
    return clean


def _merge_metadata(
    current: dict[str, object],
    incoming: dict[str, object],
) -> dict[str, object]:
    if not isinstance(current, dict) or not isinstance(incoming, dict):
        raise TypeError("_merge_metadata requires dict inputs")
    merged = dict(current)
    merged.update(dict(incoming))
    return _clean_metadata(merged)


def _coerce_confidence(value: object) -> float | None:
    return coerce_unit_float(value, digits=4)


def _int_or_default(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, str) and not value.strip().isascii():
        return default
    try:
        return int(cast(_INT_INPUT, value))
    except (TypeError, ValueError, OverflowError):
        return default
