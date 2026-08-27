"""Auditable Ghost memory inbox projection and event log."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import uuid
from typing import Iterable

from codey.ghost.gate import (
    CANDIDATE_STATUSES,
    CANDIDATE_TYPES,
    GhostGateDecision,
    GhostMemoryGate,
    candidate_type_for_signal_kind,
)
from codey.ghost.numbers import coerce_unit_float
from codey.ghost.schema import (
    SCHEMA_VERSION as SIGNAL_SCHEMA_VERSION,
    SIGNAL_KINDS,
    SIGNAL_SCOPES,
    GhostSignal,
    GhostSignalParseResult,
    clip_signal_text,
)
from codey.ghost.typed_fields import metadata_conflict_key, metadata_value_key
from codey.storage.event_state import reset_event_backed_state
from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import DEFAULT_STATE_HOME, delete_file, read_json, write_json_atomic


INBOX_SCHEMA_VERSION = 1
MAX_GHOST_EVENTS = 5_000
MAX_INBOX_ITEMS = 200
MAX_INBOX_BYTES = 2 * 1024 * 1024
MAX_EVENTS_BYTES = 4 * 1024 * 1024
MAX_CANDIDATE_METADATA_KEYS = 8
MAX_CANDIDATE_EVIDENCE_REFS = 32
MAX_EVENT_WARNINGS = 20

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+", re.IGNORECASE)
_PROJECTION_KIND = "ghost_memory_inbox_projection"
_STORED_REJECTION_REASONS = {"confidence_below_candidate_threshold"}


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
    def from_payload(cls, payload: object) -> "GhostMemoryCandidate | None":
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
        value_key = clip_signal_text(payload.get("value_key"), 180) or _legacy_value_key(summary, evidence_quote)
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
            project=_normalize_project(payload.get("project")),
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
        self.directory = Path(state_home) / "ghost"
        self.events_path = self.directory / "events.jsonl"
        self.inbox_path = self.directory / "inbox.json"
        self.settings_path = self.directory / "settings.json"
        self.gate = gate or GhostMemoryGate()
        self.last_warnings: tuple[str, ...] = ()
        self._events_read_blocked = False

    def ingest_signals(
        self,
        result: GhostSignalParseResult,
        *,
        session_id: str = "",
        run_id: str = "",
        project: str = "",
        user_text: str = "",
    ) -> tuple[GhostMemoryCandidate, ...]:
        if not self.learning_enabled():
            return ()
        signals = tuple(getattr(result, "signals", ()) or ())
        if not signals:
            return ()
        try:
            with with_file_lock(self.events_path):
                loaded = self._load_candidates()
                if self._events_read_blocked:
                    return ()
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
                    existing_index = self._find_conflict_index(candidates, candidate)
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
                if not self._append_events(events):
                    return ()
                candidates = self._bounded_candidates(candidates)
                try:
                    self._write_projection(candidates)
                except (OSError, TypeError, ValueError):
                    delete_file(self.inbox_path)
                self._compact_if_needed(candidates)
                return tuple(changed)
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
        try:
            rows = self._filter_candidates(
                self._load_candidates(),
                status=status,
                scope=scope,
                project=project,
                session_id=session_id,
                applicable=False,
            )
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
        try:
            rows = self._filter_candidates(
                self._load_candidates(),
                status=status,
                scope="",
                project=project,
                session_id=session_id,
                applicable=True,
            )
        except Exception:
            return ()
        priority = {"session": 0, "project": 1, "user": 2}
        rows.sort(key=lambda item: item.updated_at, reverse=True)
        rows.sort(key=lambda item: priority[item.scope])
        return tuple(rows)

    def export_state(self) -> dict[str, object]:
        candidates = [candidate.to_payload() for candidate in self._load_candidates()]
        events = [event for event in self._read_events()]
        return {
            "schema_version": INBOX_SCHEMA_VERSION,
            "settings": self._read_settings(),
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
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"accept", "reject"}:
            raise ValueError("action must be accept or reject")
        if not normalized_id:
            return None
        with with_file_lock(self.events_path):
            candidates = list(self._load_candidates())
            target_index: int | None = None
            for index, candidate in enumerate(candidates):
                if candidate.id == normalized_id:
                    target_index = index
                    break
            if target_index is None:
                return None
            now = _now()
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
                self._control_event(
                    "ghost_memory_candidate_reviewed",
                    {
                        "candidate_id": target.id,
                        "action": normalized_action,
                        "reviewed_by": reviewer,
                        "superseded_ids": superseded_ids,
                    },
                )
            )
            if not self._append_events(events):
                return None
            try:
                self._write_projection(candidates)
            except (OSError, TypeError, ValueError):
                delete_file(self.inbox_path)
            self._compact_if_needed(candidates)
            return target

    def reset_all(self, *, preserve_settings: bool = True) -> bool:
        try:
            state_paths = (self.inbox_path,) if preserve_settings else (self.inbox_path, self.settings_path)
            reset_event_backed_state(self.events_path, *state_paths)
            self.last_warnings = ()
            self._events_read_blocked = False
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
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope not in SIGNAL_SCOPES:
            raise ValueError("scope must be user, project, or session")
        normalized_project = _normalize_project(project)
        normalized_session = clip_signal_text(session_id, 120)
        if normalized_scope == "project" and not normalized_project:
            raise ValueError("project is required for project scope deletion")
        if normalized_scope == "session" and not normalized_session:
            raise ValueError("session_id is required for session scope deletion")
        with with_file_lock(self.events_path):
            candidates = list(self._load_candidates())
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
            control = self._control_event(
                "ghost_memory_scope_deleted",
                {
                    "scope": normalized_scope,
                    "project": normalized_project if normalized_scope == "project" else "",
                    "session_id": normalized_session if normalized_scope == "session" else "",
                    "removed_count": removed,
                },
            )
            self._rewrite_events_from_candidates(remaining, control_event=control)
            try:
                self._write_projection(remaining)
            except (OSError, TypeError, ValueError):
                delete_file(self.inbox_path)
            return removed

    def set_learning_enabled(self, enabled: bool) -> bool:
        payload = {
            "schema_version": INBOX_SCHEMA_VERSION,
            "learning_enabled": bool(enabled),
            "updated_at": _now(),
        }
        try:
            with with_file_lock(self.events_path):
                write_json_atomic(self.settings_path, payload, max_bytes=MAX_INBOX_BYTES)
                audit_ok = self._append_events(
                    [
                        self._control_event(
                            "ghost_learning_settings_updated",
                            {"learning_enabled": bool(enabled)},
                        )
                    ]
                )
        except (OSError, TypeError, ValueError):
            return False
        return audit_ok

    def learning_enabled(self) -> bool:
        return bool(self._read_settings().get("learning_enabled", True))

    def compact_if_needed(self) -> dict[str, object]:
        before = _event_file_stats(self.events_path, max_bytes=MAX_EVENTS_BYTES)
        if not before["readable"]:
            warning = str(before["warning"] or "events_unreadable")
            self.last_warnings = (warning,)
            return {
                "ok": False,
                "compacted": False,
                "events_before": before["events"],
                "events_after": before["events"],
                "bytes_before": before["bytes"],
                "bytes_after": before["bytes"],
                "warnings": [warning],
            }
        if before["events"] <= MAX_GHOST_EVENTS and before["bytes"] <= MAX_EVENTS_BYTES:
            return {
                "ok": True,
                "compacted": False,
                "events_before": before["events"],
                "events_after": before["events"],
                "bytes_before": before["bytes"],
                "bytes_after": before["bytes"],
                "warnings": list(self.last_warnings),
            }
        with with_file_lock(self.events_path):
            candidates = self._load_candidates()
            if self._events_read_blocked:
                return {
                    "ok": False,
                    "compacted": False,
                    "events_before": before["events"],
                    "events_after": before["events"],
                    "bytes_before": before["bytes"],
                    "bytes_after": before["bytes"],
                    "warnings": list(self.last_warnings),
                }
            self._compact_if_needed(candidates)
        after = _event_file_stats(self.events_path, max_bytes=MAX_EVENTS_BYTES)
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
        now = _now()
        signal_kind = str(signal.kind or "").strip().lower()
        candidate_id = "gmc_" + uuid.uuid4().hex[:24]
        return GhostMemoryCandidate(
            id=candidate_id,
            candidate_type=decision.candidate_type or candidate_type_for_signal_kind(signal_kind),
            signal_kind=signal_kind,
            status=decision.status,
            scope=str(signal.scope or "").strip().lower(),
            summary=clip_signal_text(signal.summary),
            evidence_quote=clip_signal_text(signal.evidence_quote, 240),
            confidence=_coerce_confidence(signal.confidence) or 0.0,
            conflict_key=conflict_key_for_signal(signal),
            value_key=value_key_for_signal(signal),
            session_id=clip_signal_text(session_id, 120),
            run_id=clip_signal_text(run_id, 120),
            project=_normalize_project(project),
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
    ) -> int | None:
        incoming_ref = _scope_ref(incoming)
        for index, candidate in enumerate(candidates):
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
        normalized_scope = str(scope or "").strip().lower()
        normalized_project = _normalize_project(project)
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

    def _load_candidates(self) -> tuple[GhostMemoryCandidate, ...]:
        self._events_read_blocked = False
        payload = self._read_projection_payload()
        if payload is not None:
            rows = self._candidate_rows_from_projection(payload)
            if rows is not None:
                return tuple(rows)
        rows = self._rebuild_candidates_from_events()
        if rows is None:
            return ()
        try:
            self._write_projection(rows)
        except (OSError, TypeError, ValueError):
            pass
        return tuple(rows)

    def _read_projection_payload(self) -> dict[str, object] | None:
        if not self.inbox_path.exists():
            return None
        payload = read_json(self.inbox_path, max_bytes=MAX_INBOX_BYTES)
        if not isinstance(payload, dict):
            self._quarantine(self.inbox_path)
            return None
        if payload.get("schema_version") != INBOX_SCHEMA_VERSION:
            self._quarantine(self.inbox_path)
            return None
        if payload.get("kind") != _PROJECTION_KIND:
            self._quarantine(self.inbox_path)
            return None
        return payload

    def _candidate_rows_from_projection(
        self,
        payload: dict[str, object],
    ) -> list[GhostMemoryCandidate] | None:
        raw_candidates = payload.get("candidates")
        if not isinstance(raw_candidates, list):
            return None
        rows: list[GhostMemoryCandidate] = []
        for raw in raw_candidates[:MAX_INBOX_ITEMS]:
            candidate = GhostMemoryCandidate.from_payload(raw)
            if candidate is not None:
                rows.append(candidate)
        return self._bounded_candidates(rows)

    def _rebuild_candidates_from_events(self) -> list[GhostMemoryCandidate] | None:
        by_id: dict[str, GhostMemoryCandidate] = {}
        events = self._read_events()
        if self._events_read_blocked:
            return None
        for event in events:
            event_type = str(event.get("type") or "")
            if event_type != "ghost_memory_candidate_upsert":
                continue
            candidate = GhostMemoryCandidate.from_payload(event.get("candidate"))
            if candidate is not None:
                by_id[candidate.id] = candidate
        return self._bounded_candidates(by_id.values())

    def _read_events(self) -> tuple[dict[str, object], ...]:
        warnings: list[str] = []
        self._events_read_blocked = False
        try:
            if not self.events_path.is_file():
                self.last_warnings = ()
                return ()
            if self.events_path.stat().st_size > MAX_EVENTS_BYTES:
                self.last_warnings = ("events_too_large",)
                self._events_read_blocked = True
                return ()
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            self.last_warnings = ("events_unreadable",)
            self._events_read_blocked = True
            return ()
        events: list[dict[str, object]] = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(f"events.jsonl:{index}:bad_json")
                continue
            if not isinstance(value, dict):
                warnings.append(f"events.jsonl:{index}:not_object")
                continue
            schema_version = value.get("schema_version")
            if schema_version != INBOX_SCHEMA_VERSION:
                warnings.append(f"events.jsonl:{index}:unsupported_schema")
                continue
            events.append(value)
        self.last_warnings = tuple(warnings[:MAX_EVENT_WARNINGS])
        return tuple(events)

    def _read_settings(self) -> dict[str, object]:
        default = {
            "schema_version": INBOX_SCHEMA_VERSION,
            "learning_enabled": True,
        }
        if not self.settings_path.exists():
            return default
        payload = read_json(self.settings_path, max_bytes=MAX_INBOX_BYTES)
        if not isinstance(payload, dict):
            self._quarantine(self.settings_path)
            return default
        if payload.get("schema_version") != INBOX_SCHEMA_VERSION:
            self._quarantine(self.settings_path)
            return default
        return {
            "schema_version": INBOX_SCHEMA_VERSION,
            "learning_enabled": bool(payload.get("learning_enabled", True)),
            "updated_at": clip_signal_text(payload.get("updated_at"), 80),
        }

    def _write_projection(self, candidates: Iterable[GhostMemoryCandidate]) -> None:
        payload = self._projection_payload(
            [candidate.to_payload() for candidate in self._bounded_candidates(candidates)]
        )
        write_json_atomic(self.inbox_path, payload, max_bytes=MAX_INBOX_BYTES)

    def _projection_payload(self, candidates: list[dict[str, object]]) -> dict[str, object]:
        return {
            "schema_version": INBOX_SCHEMA_VERSION,
            "kind": _PROJECTION_KIND,
            "source": "events.jsonl",
            "updated_at": _now(),
            "candidates": candidates[:MAX_INBOX_ITEMS],
            "warnings": list(self.last_warnings),
        }

    def _append_events(self, events: Iterable[dict[str, object]]) -> bool:
        rows = list(events)
        if not rows:
            return True
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                for event in rows:
                    handle.write(_json_line(event))
            return True
        except (OSError, TypeError, ValueError):
            return False

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
        rows = list(events)
        data = "".join(_json_line(event) for event in rows).encode("utf-8")
        if len(data) > MAX_EVENTS_BYTES:
            raise ValueError("ghost events are too large")
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

    def _compact_if_needed(self, candidates: Iterable[GhostMemoryCandidate]) -> None:
        try:
            event_bytes = self.events_path.stat().st_size
            if event_bytes > MAX_EVENTS_BYTES:
                line_count = MAX_GHOST_EVENTS + 1
            else:
                line_count = len(self.events_path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            return
        if line_count <= MAX_GHOST_EVENTS and event_bytes <= MAX_EVENTS_BYTES:
            return
        reason = "event_bytes_limit" if event_bytes > MAX_EVENTS_BYTES else "event_count_limit"
        try:
            self._rewrite_events_from_candidates(
                candidates,
                control_event=self._control_event(
                    "ghost_memory_store_compacted",
                    {
                        "reason": reason,
                        "max_events": MAX_GHOST_EVENTS,
                        "max_event_bytes": MAX_EVENTS_BYTES,
                    },
                ),
            )
        except (OSError, TypeError, ValueError):
            pass

    def _candidate_event(
        self,
        candidate: GhostMemoryCandidate,
        *,
        action: str,
    ) -> dict[str, object]:
        return {
            "schema_version": INBOX_SCHEMA_VERSION,
            "type": "ghost_memory_candidate_upsert",
            "ts": _now(),
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
            "ts": _now(),
            "signal_schema_version": SIGNAL_SCHEMA_VERSION,
            "signal_kind": clip_signal_text(getattr(signal, "kind", ""), 80),
            "scope": clip_signal_text(getattr(signal, "scope", ""), 40),
            "gate": decision.to_payload(),
            "stored_candidate": False,
        }

    def _control_event(self, event_type: str, payload: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": INBOX_SCHEMA_VERSION,
            "type": event_type,
            "ts": _now(),
            "payload": payload,
        }

    def _bounded_candidates(
        self,
        candidates: Iterable[GhostMemoryCandidate],
    ) -> list[GhostMemoryCandidate]:
        rows = list(candidates)
        rows.sort(key=lambda item: item.updated_at or item.created_at, reverse=True)
        return rows[:MAX_INBOX_ITEMS]

    def _quarantine(self, path: Path) -> None:
        try:
            target = path.with_name(f"{path.name}.quarantine.{_compact_timestamp()}")
            path.replace(target)
        except OSError:
            pass


def conflict_key_for_signal(signal: GhostSignal) -> str:
    kind = str(signal.kind or "").strip().lower()
    metadata_key = _metadata_conflict_key(getattr(signal, "metadata", {}) or {})
    if metadata_key:
        return f"{kind}:{metadata_key}"
    text = f"{signal.summary} {signal.evidence_quote}".casefold()
    return f"{kind}:{_token_slug(text)}"


def value_key_for_signal(signal: GhostSignal) -> str:
    metadata_key = _metadata_value_key(getattr(signal, "metadata", {}) or {})
    if metadata_key:
        return metadata_key
    return _legacy_value_key(signal.summary, signal.evidence_quote)


def _metadata_conflict_key(metadata: object) -> str:
    return metadata_conflict_key(metadata)


def _metadata_value_key(metadata: object) -> str:
    return metadata_value_key(metadata)


def _legacy_value_key(summary: object, evidence_quote: object) -> str:
    text = f"{summary} {evidence_quote}".casefold()
    return _token_slug(text)


def _token_slug(value: object) -> str:
    tokens = [token.casefold() for token in _TOKEN_RE.findall(str(value or ""))]
    if not tokens:
        return "empty"
    return "_".join(tokens[:8])[:120]


def _scope_ref(candidate: GhostMemoryCandidate) -> str:
    if candidate.scope == "project":
        return candidate.project
    if candidate.scope == "session":
        return candidate.session_id
    return ""


def _event_file_stats(path: Path, *, max_bytes: int) -> dict[str, object]:
    try:
        if not path.is_file():
            return {"events": 0, "bytes": 0, "readable": True, "warning": ""}
        event_bytes = path.stat().st_size
        if event_bytes > max(0, int(max_bytes or 0)):
            return {"events": 0, "bytes": event_bytes, "readable": True, "warning": "events_too_large"}
        event_count = len(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return {"events": 0, "bytes": 0, "readable": False, "warning": "events_unreadable"}
    return {"events": event_count, "bytes": event_bytes, "readable": True, "warning": ""}


def _scope_filter_matches(
    candidate: GhostMemoryCandidate,
    *,
    project: str,
    session_id: str,
) -> bool:
    if project and candidate.scope == "project" and candidate.project != project:
        return False
    if session_id and candidate.scope == "session" and candidate.session_id != session_id:
        return False
    return True


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
    if isinstance(value, str):
        raw_values = value.split(",")
    else:
        raw_values = list(value)
    return {str(item).strip().lower() for item in raw_values if str(item).strip().lower() in CANDIDATE_STATUSES}


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


def _normalize_project(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return clip_signal_text(Path(text).expanduser().resolve(), 240)
    except (OSError, RuntimeError, ValueError):
        return clip_signal_text(text, 240)


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
    merged = dict(current or {})
    merged.update(dict(incoming or {}))
    return _clean_metadata(merged)


def _coerce_confidence(value: object) -> float | None:
    return coerce_unit_float(value, digits=4)


def _int_or_default(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _compact_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _json_line(value: dict[str, object]) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
