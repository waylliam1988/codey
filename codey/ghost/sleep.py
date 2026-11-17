"""Local Ghost maintenance cycle.

Sleep is a short post-turn maintenance pass. It only checks, decays, refreshes
existing projections, compacts event logs, and writes a bounded report.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Protocol, SupportsIndex, SupportsInt

from codey.ghost import _common
from codey.ghost._warnings import bounded_warnings
from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.continuity import GhostContinuityStore, KnowledgeReader
from codey.ghost.events import (
    control_event as _ghost_control_event,
)
from codey.ghost.graph_primitives import parse_ts
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.payload_types import object_fields
from codey.ghost.projection_fence import fence_projection
from codey.ghost.record_store import GhostRecords
from codey.ghost.schema import clip_signal_text
from codey.ghost.sqlite import GhostSQLite
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.storage.local_store import (
    DEFAULT_STATE_HOME,
)

if TYPE_CHECKING:
    from codey.runs.ledger_projection import RunLedgerProjection


class CompactionStore(Protocol):
    def compact_if_needed(self) -> dict[str, object]: ...

SLEEP_SCHEMA_VERSION = 1
MAX_SLEEP_EVENTS = 1_000
MAX_SLEEP_EVENTS_BYTES = 512 * 1024
MAX_SLEEP_WARNINGS = 20
DEFAULT_HEBBIAN_DECAY_MIN_INTERVAL_SECONDS = 6 * 60 * 60
DEFAULT_AFFINITY_DECAY_MIN_INTERVAL_SECONDS = 6 * 60 * 60
_STATE_KIND = "ghost_sleep_state_projection"
_MAINTENANCE_STOP_REASONS = frozenset({"maintenance_disabled", "maintenance_control_failed", "cancelled"})
_STEP_NAMES: tuple[str, ...] = (
    "projection_health",
    "affinity_sync",
    "hebbian_decay",
    "affinity_decay",
    "continuity_refresh",
    "event_compaction",
    "report",
)


@dataclass(frozen=True)
class GhostSleepBudget:
    hebbian_decay_min_interval_seconds: int = DEFAULT_HEBBIAN_DECAY_MIN_INTERVAL_SECONDS
    affinity_decay_min_interval_seconds: int = DEFAULT_AFFINITY_DECAY_MIN_INTERVAL_SECONDS


@dataclass(frozen=True)
class GhostSleepCursor:
    trigger: str = "post_turn"
    run_id: str = ""
    session_id: str = ""
    project: str = ""
    run_projection: RunLedgerProjection | None = None


@dataclass(frozen=True)
class GhostSleepStepResult:
    name: str
    ok: bool
    skipped_reason: str = ""
    counts: Mapping[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    duration_ms: int = 0

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "skipped_reason": self.skipped_reason,
            "counts": {key: value for key, value in self.counts.items()},
            "warnings": list(self.warnings),
            "duration_ms": max(0, (self.duration_ms or 0)),
        }


@dataclass(frozen=True)
class GhostSleepReport:
    schema_version: int
    cycle_id: str
    trigger: str
    run_id: str
    session_id: str
    project: str
    started_at: str
    finished_at: str
    cancelled: bool
    steps: tuple[GhostSleepStepResult, ...]
    pending_steps: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    maintenance_as_of: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "cycle_id": self.cycle_id,
            "trigger": self.trigger,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "project": self.project,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "maintenance_as_of": self.maintenance_as_of,
            "cancelled": self.cancelled,
            "steps": [step.to_payload() for step in self.steps],
            "pending_steps": list(self.pending_steps),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_payload(cls, payload: object) -> GhostSleepReport | None:
        if not object_fields(payload):
            return None
        if type(payload.get("schema_version")) is not int or payload.get("schema_version") != SLEEP_SCHEMA_VERSION:
            return None
        steps = tuple(
            step for step in (_step_from_payload(row) for row in _list(payload.get("steps"))) if step is not None
        )
        cycle_id = clip_signal_text(payload.get("cycle_id"), 120)
        if not cycle_id:
            return None
        return cls(
            schema_version=SLEEP_SCHEMA_VERSION,
            cycle_id=cycle_id,
            trigger=clip_signal_text(payload.get("trigger"), 80),
            run_id=clip_signal_text(payload.get("run_id"), 120),
            session_id=clip_signal_text(payload.get("session_id"), 120),
            project=clip_signal_text(payload.get("project"), 240),
            started_at=clip_signal_text(payload.get("started_at"), 80),
            finished_at=clip_signal_text(payload.get("finished_at"), 80),
            cancelled=bool(payload.get("cancelled")),
            steps=steps,
            pending_steps=tuple(clip_signal_text(item, 80) for item in _list(payload.get("pending_steps"))),
            warnings=_bounded_warnings(_list(payload.get("warnings"))),
            maintenance_as_of=clip_signal_text(payload.get("maintenance_as_of"), 80),
        )


class GhostSleepStore:
    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.database = GhostSQLite(state_home)
        self.records = GhostRecords(self.database, "sleep")
        self.last_warnings: tuple[str, ...] = ()

    @fence_projection
    def run_once(
        self,
        *,
        inbox_store: GhostInboxStore | None = None,
        hebbian_store: GhostHebbianStore | None = None,
        continuity_store: GhostContinuityStore | None = None,
        work_queue_store: GhostWorkQueueStore | None = None,
        affinity_store: GhostAffinityStore | None = None,
        knowledge_store: KnowledgeReader | None = None,
        run_projection: RunLedgerProjection | None = None,
        trigger: str = "post_turn",
        run_id: str = "",
        session_id: str = "",
        project: str = "",
        should_cancel: Callable[[], bool] | None = None,
        budget: GhostSleepBudget | None = None,
        maintenance_as_of: str | None = None,
    ) -> GhostSleepReport:
        """Use one logical horizon; report timestamps remain actual audit time."""
        started_at = _common.now_iso_z()
        time_input = started_at if maintenance_as_of is None else maintenance_as_of
        as_of = parse_ts(time_input).isoformat().replace("+00:00", "Z")
        cursor = GhostSleepCursor(
            trigger=clip_signal_text(trigger or "post_turn", 80),
            run_id=clip_signal_text(run_id, 120),
            session_id=clip_signal_text(session_id, 120),
            project=_common.normalize_project(project),
            run_projection=run_projection,
        )
        budget = budget or GhostSleepBudget()
        controls = inbox_store if inbox_store is not None else GhostInboxStore(self.database.path.parent)
        cycle_id = "gsc_" + uuid.uuid4().hex[:24]
        steps: list[GhostSleepStepResult] = []
        pending_steps: tuple[str, ...] = ()
        cancelled = False
        step_fns: dict[str, Callable[[], GhostSleepStepResult]] = {
            "projection_health": lambda: self._projection_health_step(
                inbox_store=inbox_store,
                hebbian_store=hebbian_store,
                continuity_store=continuity_store,
                work_queue_store=work_queue_store,
                affinity_store=affinity_store,
            ),
            "affinity_sync": lambda: self._affinity_sync_step(
                inbox_store=inbox_store,
                affinity_store=affinity_store,
                hebbian_store=hebbian_store,
                work_queue_store=work_queue_store,
                run_projection=cursor.run_projection,
                session_id=cursor.session_id,
                project=cursor.project,
                maintenance_as_of=as_of,
            ),
            "hebbian_decay": lambda: self._hebbian_decay_step(
                hebbian_store,
                min_interval_seconds=budget.hebbian_decay_min_interval_seconds,
                maintenance_as_of=as_of,
            ),
            "affinity_decay": lambda: self._affinity_decay_step(
                affinity_store,
                min_interval_seconds=budget.affinity_decay_min_interval_seconds,
                maintenance_as_of=as_of,
            ),
            "continuity_refresh": lambda: self._continuity_refresh_step(
                inbox_store=inbox_store,
                continuity_store=continuity_store,
                hebbian_store=hebbian_store,
                knowledge_store=knowledge_store,
                run_projection=cursor.run_projection,
                run_id=cursor.run_id,
                session_id=cursor.session_id,
                project=cursor.project,
                maintenance_as_of=as_of,
            ),
            "event_compaction": lambda: self._event_compaction_step(
                inbox_store=inbox_store,
                hebbian_store=hebbian_store,
                continuity_store=continuity_store,
                work_queue_store=work_queue_store,
                affinity_store=affinity_store,
                maintenance_as_of=as_of,
                controls=controls,
                cursor=cursor,
                should_cancel=should_cancel,
            ),
        }
        for index, name in enumerate(_STEP_NAMES[:-1]):
            if should_cancel is not None and should_cancel():
                cancelled = True
                pending_steps = _STEP_NAMES[index:-1]
                break
            def execute_step(step_name: str = name) -> GhostSleepStepResult:
                return self._controlled_step(
                    step_name, step_fns[step_name], controls=controls, cursor=cursor,
                    read_only=step_name in {"projection_health", "event_compaction"},
                )

            result = _timed_step(name, execute_step)
            steps.append(result)
            if result.skipped_reason in _MAINTENANCE_STOP_REASONS:
                cancelled = True
                pending_steps = _STEP_NAMES[index:-1]
                break
        warnings = _bounded_warnings(warning for step in steps for warning in step.warnings)
        report_step = _timed_step("report", self._report_write_allowed_step)
        final_warnings = _bounded_warnings((*warnings, *report_step.warnings))
        final_report = GhostSleepReport(
            schema_version=SLEEP_SCHEMA_VERSION,
            cycle_id=cycle_id,
            trigger=cursor.trigger,
            run_id=cursor.run_id,
            session_id=cursor.session_id,
            project=cursor.project,
            started_at=started_at,
            finished_at=_common.now_iso_z(),
            cancelled=cancelled,
            steps=(*tuple(steps), report_step),
            pending_steps=pending_steps,
            warnings=final_warnings,
            maintenance_as_of=as_of,
        )
        if report_step.ok:
            try:
                with self.records.transaction():
                    self._append_events([_report_event(final_report)])
                    self._write_state(final_report)
                    self._compact_if_needed()
            except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
                failed_step = GhostSleepStepResult(
                    "report",
                    False,
                    skipped_reason="report_write_failed",
                    counts={"reports_written": 0},
                    warnings=(f"sleep_report_write_failed:{type(exc).__name__}",),
                )
                final_report = replace(
                    final_report,
                    steps=(*tuple(steps), failed_step),
                    warnings=_bounded_warnings((*warnings, *failed_step.warnings)),
                )
        self.last_warnings = final_report.warnings
        return final_report

    def _controlled_step(
        self, name: str, function: Callable[[], GhostSleepStepResult], *,
        controls: GhostInboxStore, cursor: GhostSleepCursor, read_only: bool = False,
    ) -> GhostSleepStepResult:
        # Serialize the authority read with its mutation, across all same-path owners.
        # Health is read-only; composite compaction admits each store independently.
        with nullcontext() if read_only else self.database.write_transaction():
            try:
                if controls.database.path.resolve() != self.database.path.resolve():
                    raise ValueError("maintenance authority belongs to another database")
                enabled = controls.control_enabled(
                    "run_maintenance", project=cursor.project, session_id=cursor.session_id,
                )
            except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
                return GhostSleepStepResult(
                    name, False, skipped_reason="maintenance_control_failed",
                    warnings=(f"sleep_maintenance_control_failed:{type(exc).__name__}",),
                )
            if not enabled:
                return GhostSleepStepResult(name, True, skipped_reason="maintenance_disabled")
            return function()

    def export_state(self) -> dict[str, object]:
        with self.records.transaction(write=False):
            events = self._read_events_unlocked()
            event_warnings = self.last_warnings
            state = self._read_state_payload_unlocked()
            warnings = _bounded_warnings((*event_warnings, *self.last_warnings))
            return {
                "sleep": state or {},
                "sleep_events": events,
                "warnings": list(warnings),
            }

    def reset_all(self) -> bool:
        self.records.reset()
        self.last_warnings = ()
        return True

    def delete_scope(
        self,
        scope: str,
        *,
        project: str = "",
        session_id: str = "",
    ) -> dict[str, int]:
        normalized_scope = (scope or "").strip().lower()
        if normalized_scope not in {"user", "project", "session"}:
            raise ValueError("scope must be user, project, or session")
        project_ref = _common.normalize_project(project)
        session_ref = clip_signal_text(session_id, 120)
        if normalized_scope == "project" and not project_ref:
            raise ValueError("project is required for project scope deletion")
        if normalized_scope == "session" and not session_ref:
            raise ValueError("session_id is required for session scope deletion")
        with self.records.transaction(deleting=True):
            events = _live_report_events(self._read_events_unlocked())
            kept: list[dict[str, object]] = []
            removed = 0
            for event in events:
                report = GhostSleepReport.from_payload(event.get("report"))
                if report is not None and _scope_matches_report(
                    report,
                    normalized_scope,
                    project=project_ref,
                    session_id=session_ref,
                ):
                    removed += 1
                    continue
                kept.append(event)
            current = self._read_report_unlocked()
            state_deleted = 0
            if current is not None and _scope_matches_report(
                current,
                normalized_scope,
                project=project_ref,
                session_id=session_ref,
            ):
                state_deleted = 1
            if removed:
                control = _ghost_control_event(
                    schema_version=SLEEP_SCHEMA_VERSION,
                    event_name="ghost_sleep_scope_deleted",
                    event_id_prefix="gsd_",
                    payload={
                        "scope": normalized_scope,
                        "project": project_ref if normalized_scope == "project" else "",
                        "session_id": session_ref if normalized_scope == "session" else "",
                        "removed_reports": removed,
                    },
                    now=_common.now_iso_z(),
                )
                self._append_events([control])
            if state_deleted:
                latest = _latest_report_from_events(kept)
                if latest is None:
                    self.records.replace_rows(())
                else:
                    self._write_state(latest)
            self.records.mark_applied()
            return {"reports": removed, "state_deleted": state_deleted}

    def _projection_health_step(
        self,
        *,
        inbox_store: GhostInboxStore | None,
        hebbian_store: GhostHebbianStore | None,
        continuity_store: GhostContinuityStore | None,
        work_queue_store: GhostWorkQueueStore | None,
        affinity_store: GhostAffinityStore | None,
    ) -> GhostSleepStepResult:
        with self.database.connection() as database:
            database.execute("BEGIN")
            check_rows: Iterable[sqlite3.Row] = database.execute("PRAGMA quick_check")
            checks: list[str] = []
            for row in check_rows:
                check: object = row[0]
                if not isinstance(check, str):
                    raise ValueError("invalid Ghost integrity check result")
                checks.append(check)
            foreign_keys: list[sqlite3.Row] = database.execute("PRAGMA foreign_key_check").fetchall()
            cursor_row: sqlite3.Row | None = database.execute(
                "WITH owners AS (SELECT collection FROM memory_consumers UNION SELECT collection FROM memory_events) "
                "SELECT count(*) FROM owners o LEFT JOIN memory_consumers c USING(collection) "
                "WHERE coalesce(c.applied_sequence,0) != "
                "(SELECT coalesce(max(sequence),0) FROM memory_events e WHERE e.collection=o.collection)"
            ).fetchone()
            cursors: object = None
            if cursor_row is not None:
                cursors = cursor_row[0]
            graph_row: sqlite3.Row | None = database.execute(
                "WITH owners AS (SELECT graph FROM graph_consumers UNION SELECT graph FROM graph_events) "
                "SELECT count(*) FROM owners o LEFT JOIN graph_consumers c USING(graph) "
                "WHERE coalesce(c.applied_sequence,0) != "
                "(SELECT coalesce(max(sequence),0) FROM graph_events e WHERE e.graph=o.graph)"
            ).fetchone()
            graph_cursors: object = None
            if graph_row is not None:
                graph_cursors = graph_row[0]
            if type(cursors) is not int or type(graph_cursors) is not int:
                raise ValueError("invalid Ghost projection health counts")
        warnings = []
        if checks != ["ok"]:
            warnings.append("ghost_integrity_failed")
        if foreign_keys:
            warnings.append("ghost_foreign_keys_failed")
        if cursors or graph_cursors:
            warnings.append("ghost_consumer_cursor_mismatch")
        return GhostSleepStepResult(
            "projection_health",
            not warnings,
            counts={
                "databases_checked": 1,
                "invalid_cursors": cursors + graph_cursors,
                "foreign_key_errors": len(foreign_keys),
            },
            warnings=tuple(warnings),
        )

    def _affinity_sync_step(
        self,
        *,
        inbox_store: GhostInboxStore | None,
        affinity_store: GhostAffinityStore | None,
        hebbian_store: GhostHebbianStore | None,
        work_queue_store: GhostWorkQueueStore | None,
        run_projection: RunLedgerProjection | None,
        session_id: str,
        project: str,
        maintenance_as_of: str,
    ) -> GhostSleepStepResult:
        if affinity_store is None:
            return GhostSleepStepResult("affinity_sync", True, skipped_reason="store_unavailable")
        allowed, control_warnings = (
            _external_source_permission(inbox_store, session_id=session_id, project=project)
            if run_projection is not None else (True, ())
        )
        result = affinity_store.sync_from_sources(
            hebbian_store=hebbian_store,
            work_queue_store=work_queue_store,
            run_projection=run_projection if allowed else None,
            session_id=session_id,
            project=project,
            maintenance_as_of=maintenance_as_of,
        )
        return GhostSleepStepResult(
            "affinity_sync",
            result.ok,
            skipped_reason=clip_signal_text(result.skipped_reason, 80),
            counts={
                "nodes_changed": result.nodes_changed,
                "edges_changed": result.edges_changed,
                "total_nodes": result.total_nodes,
                "total_edges": result.total_edges,
                "external_sources_allowed": int(allowed),
            },
            warnings=_bounded_warnings((*control_warnings, *result.warnings)),
        )

    def _hebbian_decay_step(
        self,
        hebbian_store: GhostHebbianStore | None,
        *,
        min_interval_seconds: int,
        maintenance_as_of: str,
    ) -> GhostSleepStepResult:
        if hebbian_store is None:
            return GhostSleepStepResult("hebbian_decay", True, skipped_reason="store_unavailable")
        result = hebbian_store.decay(min_interval_seconds=min_interval_seconds, maintenance_as_of=maintenance_as_of)
        counts = {
            "removed_nodes": _int(result.get("removed_nodes")),
            "removed_edges": _int(result.get("removed_edges")),
            "decayed_nodes": _int(result.get("decayed_nodes")),
            "decayed_edges": _int(result.get("decayed_edges")),
        }
        return GhostSleepStepResult(
            "hebbian_decay",
            True,
            skipped_reason=clip_signal_text(result.get("skipped_reason"), 80),
            counts=counts,
            warnings=_bounded_warnings(result.get("warnings", ())),
        )

    def _affinity_decay_step(
        self,
        affinity_store: GhostAffinityStore | None,
        *,
        min_interval_seconds: int,
        maintenance_as_of: str,
    ) -> GhostSleepStepResult:
        if affinity_store is None:
            return GhostSleepStepResult("affinity_decay", True, skipped_reason="store_unavailable")
        result = affinity_store.decay(min_interval_seconds=min_interval_seconds, maintenance_as_of=maintenance_as_of)
        return GhostSleepStepResult(
            "affinity_decay",
            True,
            skipped_reason=clip_signal_text(result.get("skipped_reason"), 80),
            counts={
                "removed_nodes": _int(result.get("removed_nodes")),
                "removed_edges": _int(result.get("removed_edges")),
                "decayed_nodes": _int(result.get("decayed_nodes")),
                "decayed_edges": _int(result.get("decayed_edges")),
            },
            warnings=_bounded_warnings(result.get("warnings", ())),
        )

    def _continuity_refresh_step(
        self,
        *,
        inbox_store: GhostInboxStore | None,
        continuity_store: GhostContinuityStore | None,
        hebbian_store: GhostHebbianStore | None,
        knowledge_store: KnowledgeReader | None,
        run_projection: RunLedgerProjection | None,
        run_id: str,
        session_id: str,
        project: str,
        maintenance_as_of: str,
    ) -> GhostSleepStepResult:
        if continuity_store is None:
            return GhostSleepStepResult("continuity_refresh", True, skipped_reason="store_unavailable")
        allowed, control_warnings = (
            _external_source_permission(inbox_store, session_id=session_id, project=project)
            if run_projection is not None or knowledge_store is not None else (True, ())
        )
        result = continuity_store.sync_from_sources(
            hebbian_store=hebbian_store,
            run_projection=run_projection if allowed else None,
            knowledge_store=knowledge_store if allowed else None,
            user_focus_excerpt="",
            session_id=session_id,
            run_id=run_id,
            project=project,
            mode="sleep",
            maintenance_as_of=maintenance_as_of,
        )
        return GhostSleepStepResult(
            "continuity_refresh",
            result.ok,
            skipped_reason=clip_signal_text(result.skipped_reason, 80),
            counts={
                "items_changed": result.items_changed,
                "total_items": result.total_items,
                "external_sources_allowed": int(allowed),
            },
            warnings=_bounded_warnings((*control_warnings, *result.warnings)),
        )

    def _event_compaction_step(
        self,
        *,
        inbox_store: GhostInboxStore | None,
        hebbian_store: GhostHebbianStore | None,
        continuity_store: GhostContinuityStore | None,
        work_queue_store: GhostWorkQueueStore | None,
        affinity_store: GhostAffinityStore | None,
        maintenance_as_of: str,
        controls: GhostInboxStore,
        cursor: GhostSleepCursor,
        should_cancel: Callable[[], bool] | None,
    ) -> GhostSleepStepResult:
        warnings: list[str] = []
        counts = {"stores_checked": 0, "stores_compacted": 0, "stores_failed": 0, "stale_work_claims": 0}
        targets: tuple[tuple[str, CompactionStore | None], ...] = (
            ("inbox", inbox_store),
            ("hebbian", hebbian_store),
            ("continuity", continuity_store),
            ("work_queue", work_queue_store),
            ("affinity", affinity_store),
        )
        for name, store in targets:
            if store is None:
                continue
            if should_cancel is not None and should_cancel():
                return GhostSleepStepResult(
                    "event_compaction", counts["stores_failed"] == 0, skipped_reason="cancelled",
                    counts=counts, warnings=_bounded_warnings(warnings),
                )

            target_store: CompactionStore = store

            def compact(target_name: str = name, target: CompactionStore = target_store) -> GhostSleepStepResult:
                return _compact_target(target_name, target, work_queue_store, maintenance_as_of)

            try:
                result = self._controlled_step(name, compact, controls=controls, cursor=cursor)
            except Exception as exc:
                counts["stores_checked"] += 1
                counts["stores_failed"] += 1
                warnings.append(f"{name}_compact_error:{type(exc).__name__}")
                continue
            warnings.extend(result.warnings)
            for key, count in result.counts.items():
                counts[key] += count
            if result.skipped_reason in _MAINTENANCE_STOP_REASONS:
                return replace(result, name="event_compaction", counts=counts, warnings=_bounded_warnings(warnings))
        return GhostSleepStepResult(
            "event_compaction",
            counts["stores_failed"] == 0,
            counts=counts,
            warnings=_bounded_warnings(warnings),
        )

    def _report_write_allowed_step(self) -> GhostSleepStepResult:
        self.records.rows()
        return GhostSleepStepResult("report", True, counts={"reports_written": 1})

    def _read_report_unlocked(self) -> GhostSleepReport | None:
        payload = self._read_state_payload_unlocked()
        if payload is None:
            return None
        return GhostSleepReport.from_payload(payload.get("report"))

    def _read_state_payload_unlocked(self) -> dict[str, object] | None:
        return self.records.payload()

    def _write_state(self, report: GhostSleepReport) -> None:
        self.records.replace_rows(
            [
                {
                    "id": "latest",
                    "scope": "session",
                    "scope_ref": report.session_id,
                    "payload": {
                        "schema_version": SLEEP_SCHEMA_VERSION,
                        "kind": _STATE_KIND,
                        "source": "ghost.sqlite3:sleep",
                        "updated_at": _common.now_iso_z(),
                        "report": report.to_payload(),
                        "warnings": list(report.warnings),
                    },
                }
            ]
        )

    def _read_events_unlocked(self) -> list[dict[str, object]]:
        return self.records.events()

    def _append_events(self, events: Iterable[dict[str, object]]) -> None:
        self.records.append(events)

    def _compact_if_needed(self) -> None:
        stats = self.records.event_stats()
        if stats["events"] > MAX_SLEEP_EVENTS or stats["bytes"] > MAX_SLEEP_EVENTS_BYTES:
            self.records.retain_recent_events(max_events=MAX_SLEEP_EVENTS, max_bytes=MAX_SLEEP_EVENTS_BYTES)


def _compact_target(
    name: str, store: CompactionStore, work_queue: GhostWorkQueueStore | None, maintenance_as_of: str,
) -> GhostSleepStepResult:
    warnings: list[str] = []
    counts = {"stores_checked": 1, "stores_compacted": 0, "stores_failed": 0, "stale_work_claims": 0}
    if name == "work_queue" and work_queue is not None:
        # Reconciliation and compaction share the admitted transaction. Exceptions
        # escape so a failed compaction cannot commit only the lease transition.
        result = work_queue.reconcile_stale_claims(maintenance_as_of=maintenance_as_of)
        counts["stores_failed"] += int(not result.ok)
        counts["stale_work_claims"] += result.items_changed
        warnings.extend(result.warnings)
    compacted = store.compact_if_needed()
    counts["stores_failed"] += int(not bool(compacted.get("ok", True)))
    counts["stores_compacted"] += int(bool(compacted.get("compacted")))
    warnings.extend(_bounded_warnings(compacted.get("warnings", ())))
    return GhostSleepStepResult(name, counts["stores_failed"] == 0, counts=counts, warnings=_bounded_warnings(warnings))


def _external_source_permission(
    inbox: GhostInboxStore | None, *, session_id: str, project: str,
) -> tuple[bool, tuple[str, ...]]:
    if inbox is None:
        return False, ("sleep_source_control_unavailable",)
    try:
        return inbox.control_enabled("accept_events", session_id=session_id, project=project), ()
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        return False, (f"sleep_source_control_failed:{type(exc).__name__}",)


def _timed_step(name: str, fn: Callable[[], GhostSleepStepResult]) -> GhostSleepStepResult:
    started = perf_counter()
    try:
        result = fn()
    except Exception as exc:
        result = GhostSleepStepResult(
            name,
            False,
            skipped_reason="step_error",
            warnings=(f"{name}:{type(exc).__name__}",),
        )
    elapsed = int((perf_counter() - started) * 1000)
    return GhostSleepStepResult(
        name=result.name or name,
        ok=result.ok,
        skipped_reason=result.skipped_reason,
        counts=result.counts,
        warnings=result.warnings,
        duration_ms=elapsed,
    )


def _step_from_payload(payload: object) -> GhostSleepStepResult | None:
    if not object_fields(payload):
        return None
    name = clip_signal_text(payload.get("name"), 80)
    if not name:
        return None
    raw_counts = payload.get("counts")
    counts_source = raw_counts if object_fields(raw_counts) else dict[str, object]()
    counts = {clip_signal_text(key, 80): _int(value) for key, value in counts_source.items()}
    return GhostSleepStepResult(
        name=name,
        ok=bool(payload.get("ok")),
        skipped_reason=clip_signal_text(payload.get("skipped_reason"), 80),
        counts=counts,
        warnings=_bounded_warnings(_list(payload.get("warnings"))),
        duration_ms=_int(payload.get("duration_ms")),
    )


def _report_event(report: GhostSleepReport) -> dict[str, object]:
    return {
        "schema_version": SLEEP_SCHEMA_VERSION,
        "type": "ghost_sleep_report",
        "ts": _common.now_iso_z(),
        "report": report.to_payload(),
    }


def _latest_report_from_events(events: Iterable[dict[str, object]]) -> GhostSleepReport | None:
    latest: GhostSleepReport | None = None
    for event in _live_report_events(events):
        if event.get("type") != "ghost_sleep_report":
            continue
        report = GhostSleepReport.from_payload(event.get("report"))
        if report is not None:
            latest = report
    return latest


def _live_report_events(events: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    for event in events:
        if event.get("type") == "ghost_sleep_report":
            if GhostSleepReport.from_payload(event.get("report")) is not None:
                reports.append(event)
        elif event.get("type") == "ghost_sleep_scope_deleted":
            payload = event.get("payload")
            if not object_fields(payload):
                raise ValueError("invalid Ghost Sleep scope deletion")
            scope = payload.get("scope")
            project = payload.get("project", "")
            session_id = payload.get("session_id", "")
            if (not isinstance(scope, str) or scope not in {"user", "project", "session"}
                    or not isinstance(project, str) or not isinstance(session_id, str)
                    or (scope == "project" and not project) or (scope == "session" and not session_id)):
                raise ValueError("invalid Ghost Sleep deletion scope")
            kept: list[dict[str, object]] = []
            for previous in reports:
                report = GhostSleepReport.from_payload(previous.get("report"))
                if report is not None and not _scope_matches_report(
                    report, scope, project=project, session_id=session_id,
                ):
                    kept.append(previous)
            reports = kept
    return reports


def _scope_matches_report(
    report: GhostSleepReport,
    scope: str,
    *,
    project: str,
    session_id: str,
) -> bool:
    if scope == "user":
        return True
    if scope == "project":
        return bool(project) and report.project == project
    if scope == "session":
        return bool(session_id) and report.session_id == session_id
    return False


def _bounded_warnings(warnings: object) -> tuple[str, ...]:
    return bounded_warnings(warnings, limit=MAX_SLEEP_WARNINGS, redact_sensitive=True)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        return []
    items: Iterable[object] = value
    return list(items)


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        if not isinstance(value, (str, bytes, bytearray, SupportsInt, SupportsIndex)):
            return 0
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


__all__ = [
    "GhostSleepBudget",
    "GhostSleepCursor",
    "GhostSleepReport",
    "GhostSleepStepResult",
    "GhostSleepStore",
    "SLEEP_SCHEMA_VERSION",
]
