"""Local Ghost maintenance cycle.

Sleep is a short post-turn maintenance pass. It only checks, decays, refreshes
existing projections, compacts event logs, and writes a bounded report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterable, Mapping
import uuid

from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.continuity import GhostContinuityStore
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.schema import clip_signal_text, contains_sensitive_signal_text
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.storage.event_state import reset_event_backed_state
from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import DEFAULT_STATE_HOME, delete_file, read_json, write_json_atomic


SLEEP_SCHEMA_VERSION = 1
MAX_SLEEP_EVENTS = 1_000
MAX_SLEEP_STATE_BYTES = 512 * 1024
MAX_SLEEP_EVENTS_BYTES = 512 * 1024
MAX_SLEEP_WARNINGS = 20
DEFAULT_HEBBIAN_DECAY_MIN_INTERVAL_SECONDS = 6 * 60 * 60
DEFAULT_AFFINITY_DECAY_MIN_INTERVAL_SECONDS = 6 * 60 * 60
_STATE_KIND = "ghost_sleep_state_projection"
_STEP_NAMES = (
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
    run_projection: Any = None


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
            "counts": {str(key): int(value) for key, value in self.counts.items()},
            "warnings": list(self.warnings),
            "duration_ms": max(0, int(self.duration_ms or 0)),
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
            "cancelled": self.cancelled,
            "steps": [step.to_payload() for step in self.steps],
            "pending_steps": list(self.pending_steps),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "GhostSleepReport | None":
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != SLEEP_SCHEMA_VERSION:
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
        )


class GhostSleepStore:
    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.directory = Path(state_home) / "ghost"
        self.state_path = self.directory / "sleep_state.json"
        self.events_path = self.directory / "sleep_events.jsonl"
        self.last_warnings: tuple[str, ...] = ()
        self._events_read_blocked = False

    def run_once(
        self,
        *,
        inbox_store: GhostInboxStore | None = None,
        hebbian_store: GhostHebbianStore | None = None,
        continuity_store: GhostContinuityStore | None = None,
        work_queue_store: GhostWorkQueueStore | None = None,
        affinity_store: GhostAffinityStore | None = None,
        router_store: Any = None,
        knowledge_store: Any = None,
        run_projection: Any = None,
        trigger: str = "post_turn",
        run_id: str = "",
        session_id: str = "",
        project: str = "",
        should_cancel: Callable[[], bool] | None = None,
        budget: GhostSleepBudget | None = None,
    ) -> GhostSleepReport:
        cursor = GhostSleepCursor(
            trigger=clip_signal_text(trigger or "post_turn", 80),
            run_id=clip_signal_text(run_id, 120),
            session_id=clip_signal_text(session_id, 120),
            project=_normalize_project(project),
            run_projection=run_projection,
        )
        budget = budget or GhostSleepBudget()
        cycle_id = "gsc_" + uuid.uuid4().hex[:24]
        started_at = _now()
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
                affinity_store=affinity_store,
                hebbian_store=hebbian_store,
                work_queue_store=work_queue_store,
                router_store=router_store,
                run_projection=cursor.run_projection,
                session_id=cursor.session_id,
                project=cursor.project,
            ),
            "hebbian_decay": lambda: self._hebbian_decay_step(
                hebbian_store,
                min_interval_seconds=budget.hebbian_decay_min_interval_seconds,
            ),
            "affinity_decay": lambda: self._affinity_decay_step(
                affinity_store,
                min_interval_seconds=budget.affinity_decay_min_interval_seconds,
            ),
            "continuity_refresh": lambda: self._continuity_refresh_step(
                continuity_store=continuity_store,
                hebbian_store=hebbian_store,
                knowledge_store=knowledge_store,
                run_projection=cursor.run_projection,
                run_id=cursor.run_id,
                session_id=cursor.session_id,
                project=cursor.project,
            ),
            "event_compaction": lambda: self._event_compaction_step(
                inbox_store=inbox_store,
                hebbian_store=hebbian_store,
                continuity_store=continuity_store,
                work_queue_store=work_queue_store,
                affinity_store=affinity_store,
            ),
        }
        for index, name in enumerate(_STEP_NAMES[:-1]):
            if should_cancel is not None and should_cancel():
                cancelled = True
                pending_steps = _STEP_NAMES[index:-1]
                break
            steps.append(_timed_step(name, step_fns[name]))
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
            finished_at=_now(),
            cancelled=cancelled,
            steps=(*tuple(steps), report_step),
            pending_steps=pending_steps,
            warnings=final_warnings,
        )
        if report_step.ok:
            with with_file_lock(self.events_path):
                if self._append_events([_report_event(final_report)]):
                    try:
                        self._write_projection(final_report)
                    except (OSError, TypeError, ValueError):
                        pass
                    self._compact_if_needed()
                else:
                    failed_step = GhostSleepStepResult(
                        "report",
                        False,
                        skipped_reason="event_write_failed",
                        counts={"reports_written": 0},
                        warnings=("sleep_event_write_failed",),
                        duration_ms=report_step.duration_ms,
                    )
                    final_report = GhostSleepReport(
                        schema_version=SLEEP_SCHEMA_VERSION,
                        cycle_id=cycle_id,
                        trigger=cursor.trigger,
                        run_id=cursor.run_id,
                        session_id=cursor.session_id,
                        project=cursor.project,
                        started_at=started_at,
                        finished_at=_now(),
                        cancelled=cancelled,
                        steps=(*tuple(steps), failed_step),
                        pending_steps=pending_steps,
                        warnings=_bounded_warnings((*warnings, *failed_step.warnings)),
                    )
        self.last_warnings = final_report.warnings
        return final_report

    def export_state(self) -> dict[str, object]:
        with with_file_lock(self.events_path):
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
        try:
            reset_event_backed_state(self.events_path, self.state_path)
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
    ) -> dict[str, int]:
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope not in {"user", "project", "session"}:
            raise ValueError("scope must be user, project, or session")
        project_ref = _normalize_project(project)
        session_ref = clip_signal_text(session_id, 120)
        if normalized_scope == "project" and not project_ref:
            raise ValueError("project is required for project scope deletion")
        if normalized_scope == "session" and not session_ref:
            raise ValueError("session_id is required for session scope deletion")
        with with_file_lock(self.events_path):
            events = self._read_events_unlocked()
            if self._events_read_blocked:
                raise OSError("ghost sleep events are unreadable")
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
                control = _control_event(
                    "ghost_sleep_scope_deleted",
                    {
                        "scope": normalized_scope,
                        "project": project_ref if normalized_scope == "project" else "",
                        "session_id": session_ref if normalized_scope == "session" else "",
                        "removed_reports": removed,
                    },
                )
                self._write_events_atomic([*kept, control])
            if state_deleted:
                latest = _latest_report_from_events(kept)
                if latest is None:
                    delete_file(self.state_path)
                else:
                    self._write_projection(latest)
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
        specs = [
            ("inbox_projection", getattr(inbox_store, "inbox_path", None), 2 * 1024 * 1024, "json"),
            ("inbox_events", getattr(inbox_store, "events_path", None), 4 * 1024 * 1024, "jsonl"),
            ("hebbian_projection", getattr(hebbian_store, "state_path", None), 4 * 1024 * 1024, "json"),
            ("hebbian_events", getattr(hebbian_store, "events_path", None), 4 * 1024 * 1024, "jsonl"),
            ("continuity_projection", getattr(continuity_store, "projection_path", None), 1024 * 1024, "json"),
            ("continuity_events", getattr(continuity_store, "events_path", None), 1024 * 1024, "jsonl"),
            ("work_queue_projection", getattr(work_queue_store, "projection_path", None), 1024 * 1024, "json"),
            ("work_queue_events", getattr(work_queue_store, "events_path", None), 1024 * 1024, "jsonl"),
            ("affinity_projection", getattr(affinity_store, "projection_path", None), 1024 * 1024, "json"),
            ("affinity_events", getattr(affinity_store, "events_path", None), 1024 * 1024, "jsonl"),
            ("sleep_projection", self.state_path, MAX_SLEEP_STATE_BYTES, "json"),
            ("sleep_events", self.events_path, MAX_SLEEP_EVENTS_BYTES, "jsonl"),
        ]
        warnings: list[str] = []
        counts = {"checked": 0, "present": 0, "missing": 0, "unreadable": 0, "too_large": 0}
        for name, path_obj, max_bytes, kind in specs:
            if not isinstance(path_obj, Path):
                continue
            result = _probe_file(path_obj, max_bytes=max_bytes, kind=kind)
            counts["checked"] += 1
            counts[result] = counts.get(result, 0) + 1
            if result in {"unreadable", "too_large"}:
                warnings.append(f"{name}_{result}")
        return GhostSleepStepResult(
            name="projection_health",
            ok=counts["unreadable"] == 0 and counts["too_large"] == 0,
            counts=counts,
            warnings=_bounded_warnings(warnings),
        )

    def _affinity_sync_step(
        self,
        *,
        affinity_store: GhostAffinityStore | None,
        hebbian_store: GhostHebbianStore | None,
        work_queue_store: GhostWorkQueueStore | None,
        router_store: Any,
        run_projection: Any,
        session_id: str,
        project: str,
    ) -> GhostSleepStepResult:
        if affinity_store is None:
            return GhostSleepStepResult("affinity_sync", True, skipped_reason="store_unavailable")
        result = affinity_store.sync_from_sources(
            hebbian_store=hebbian_store,
            work_queue_store=work_queue_store,
            router_store=router_store,
            run_projection=run_projection,
            session_id=session_id,
            project=project,
        )
        return GhostSleepStepResult(
            "affinity_sync",
            result.ok,
            skipped_reason=clip_signal_text(result.skipped_reason, 80),
            counts={
                "nodes_changed": int(result.nodes_changed),
                "edges_changed": int(result.edges_changed),
                "total_nodes": int(result.total_nodes),
                "total_edges": int(result.total_edges),
            },
            warnings=_bounded_warnings(result.warnings),
        )

    def _hebbian_decay_step(
        self,
        hebbian_store: GhostHebbianStore | None,
        *,
        min_interval_seconds: int,
    ) -> GhostSleepStepResult:
        if hebbian_store is None:
            return GhostSleepStepResult("hebbian_decay", True, skipped_reason="store_unavailable")
        result = hebbian_store.decay(min_interval_seconds=min_interval_seconds)
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
    ) -> GhostSleepStepResult:
        if affinity_store is None:
            return GhostSleepStepResult("affinity_decay", True, skipped_reason="store_unavailable")
        result = affinity_store.decay(min_interval_seconds=min_interval_seconds)
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
        continuity_store: GhostContinuityStore | None,
        hebbian_store: GhostHebbianStore | None,
        knowledge_store: Any,
        run_projection: Any,
        run_id: str,
        session_id: str,
        project: str,
    ) -> GhostSleepStepResult:
        if continuity_store is None:
            return GhostSleepStepResult("continuity_refresh", True, skipped_reason="store_unavailable")
        result = continuity_store.sync_from_sources(
            hebbian_store=hebbian_store,
            run_projection=run_projection,
            knowledge_store=knowledge_store,
            user_focus_excerpt="",
            session_id=session_id,
            run_id=run_id,
            project=project,
            mode="sleep",
        )
        return GhostSleepStepResult(
            "continuity_refresh",
            result.ok,
            skipped_reason=clip_signal_text(result.skipped_reason, 80),
            counts={
                "items_changed": int(result.items_changed),
                "total_items": int(result.total_items),
            },
            warnings=_bounded_warnings(result.warnings),
        )

    def _event_compaction_step(
        self,
        *,
        inbox_store: GhostInboxStore | None,
        hebbian_store: GhostHebbianStore | None,
        continuity_store: GhostContinuityStore | None,
        work_queue_store: GhostWorkQueueStore | None,
        affinity_store: GhostAffinityStore | None,
    ) -> GhostSleepStepResult:
        warnings: list[str] = []
        counts = {"stores_checked": 0, "stores_compacted": 0, "stores_failed": 0, "stale_work_claims": 0}
        for name, store in (
            ("inbox", inbox_store),
            ("hebbian", hebbian_store),
            ("continuity", continuity_store),
            ("work_queue", work_queue_store),
            ("affinity", affinity_store),
        ):
            compact = getattr(store, "compact_if_needed", None)
            if compact is None:
                continue
            counts["stores_checked"] += 1
            reconcile = getattr(store, "reconcile_stale_claims", None)
            if callable(reconcile):
                try:
                    reconcile_result = reconcile()
                except Exception as exc:
                    counts["stores_failed"] += 1
                    warnings.append(f"{name}_reconcile_error:{type(exc).__name__}")
                    continue
                if not bool(getattr(reconcile_result, "ok", True)):
                    counts["stores_failed"] += 1
                counts["stale_work_claims"] += int(getattr(reconcile_result, "items_changed", 0) or 0)
                warnings.extend(str(item) for item in getattr(reconcile_result, "warnings", ()) if item)
            try:
                result = compact()
            except Exception as exc:
                counts["stores_failed"] += 1
                warnings.append(f"{name}_compact_error:{type(exc).__name__}")
                continue
            if not bool(result.get("ok", True)):
                counts["stores_failed"] += 1
            if bool(result.get("compacted")):
                counts["stores_compacted"] += 1
            warnings.extend(str(item) for item in result.get("warnings", ()) if item)
        return GhostSleepStepResult(
            "event_compaction",
            counts["stores_failed"] == 0,
            counts=counts,
            warnings=_bounded_warnings(warnings),
        )

    def _report_write_allowed_step(self) -> GhostSleepStepResult:
        with with_file_lock(self.events_path):
            if self.events_path.exists():
                self._read_events_unlocked()
                if self._events_read_blocked:
                    return GhostSleepStepResult(
                        "report",
                        False,
                        skipped_reason="events_read_blocked",
                        counts={"reports_written": 0},
                        warnings=self.last_warnings,
                    )
            return GhostSleepStepResult("report", True, counts={"reports_written": 1})

    def _read_report_unlocked(self) -> GhostSleepReport | None:
        payload = self._read_state_payload_unlocked()
        if payload is None:
            return None
        return GhostSleepReport.from_payload(payload.get("report"))

    def _read_state_payload_unlocked(self) -> dict[str, object] | None:
        payload = read_json(self.state_path, max_bytes=MAX_SLEEP_STATE_BYTES)
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != SLEEP_SCHEMA_VERSION:
            return None
        if payload.get("kind") != _STATE_KIND:
            return None
        return payload

    def _write_projection(self, report: GhostSleepReport) -> None:
        write_json_atomic(
            self.state_path,
            {
                "schema_version": SLEEP_SCHEMA_VERSION,
                "kind": _STATE_KIND,
                "source": "sleep_events.jsonl",
                "updated_at": _now(),
                "report": report.to_payload(),
                "warnings": list(report.warnings),
            },
            max_bytes=MAX_SLEEP_STATE_BYTES,
        )

    def _read_events_unlocked(self) -> list[dict[str, object]]:
        self._events_read_blocked = False
        try:
            if not self.events_path.is_file():
                self.last_warnings = ()
                return []
            if self.events_path.stat().st_size > MAX_SLEEP_EVENTS_BYTES:
                self.last_warnings = ("sleep_events_too_large",)
                self._events_read_blocked = True
                return []
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            self.last_warnings = ("sleep_events_unreadable",)
            self._events_read_blocked = True
            return []
        rows: list[dict[str, object]] = []
        warnings: list[str] = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(f"sleep_events.jsonl:{index}:bad_json")
                continue
            if isinstance(payload, dict) and payload.get("schema_version") == SLEEP_SCHEMA_VERSION:
                rows.append(payload)
        self.last_warnings = _bounded_warnings(warnings)
        return rows

    def _append_events(self, events: Iterable[dict[str, object]]) -> bool:
        rows = [event for event in events if isinstance(event, dict)]
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

    def _write_events_atomic(self, events: Iterable[dict[str, object]]) -> None:
        rows = [event for event in events if isinstance(event, dict)]
        data = "".join(_json_line(event) for event in rows).encode("utf-8")
        if len(data) > MAX_SLEEP_EVENTS_BYTES:
            raise ValueError("ghost sleep events are too large")
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

    def _compact_if_needed(self) -> None:
        with with_file_lock(self.events_path):
            try:
                event_bytes = self.events_path.stat().st_size
                if event_bytes > MAX_SLEEP_EVENTS_BYTES:
                    event_count = MAX_SLEEP_EVENTS + 1
                else:
                    event_count = len(self.events_path.read_text(encoding="utf-8").splitlines())
            except (OSError, UnicodeDecodeError):
                return
            if event_count <= MAX_SLEEP_EVENTS and event_bytes <= MAX_SLEEP_EVENTS_BYTES:
                return
            events = self._read_events_unlocked()
            if self._events_read_blocked:
                return
            self._write_events_atomic(events[-MAX_SLEEP_EVENTS:])


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
    if not isinstance(payload, dict):
        return None
    name = clip_signal_text(payload.get("name"), 80)
    if not name:
        return None
    counts = {
        clip_signal_text(key, 80): _int(value)
        for key, value in (payload.get("counts") if isinstance(payload.get("counts"), dict) else {}).items()
    }
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
        "ts": _now(),
        "report": report.to_payload(),
    }


def _control_event(event_type: str, payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema_version": SLEEP_SCHEMA_VERSION,
        "type": event_type,
        "ts": _now(),
        "payload": dict(payload),
    }


def _latest_report_from_events(events: Iterable[dict[str, object]]) -> GhostSleepReport | None:
    latest: GhostSleepReport | None = None
    for event in events:
        if event.get("type") != "ghost_sleep_report":
            continue
        report = GhostSleepReport.from_payload(event.get("report"))
        if report is not None:
            latest = report
    return latest


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


def _normalize_project(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return clip_signal_text(Path(text).expanduser().resolve(), 240)
    except (OSError, RuntimeError, ValueError):
        return clip_signal_text(text, 240)


def _probe_file(path: Path, *, max_bytes: int, kind: str) -> str:
    try:
        if not path.exists():
            return "missing"
        if path.stat().st_size > max_bytes:
            return "too_large"
        if kind == "json":
            text = path.read_text(encoding="utf-8")
            if text.strip():
                json.loads(text)
        elif kind == "jsonl":
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    json.loads(line)
        return "present"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "unreadable"


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _bounded_warnings(warnings: Iterable[object]) -> tuple[str, ...]:
    out: list[str] = []
    for warning in warnings:
        text = clip_signal_text(warning, 180)
        if not text or contains_sensitive_signal_text(text):
            text = "redacted_warning"
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_SLEEP_WARNINGS:
            break
    return tuple(out)


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "GhostSleepBudget",
    "GhostSleepCursor",
    "GhostSleepReport",
    "GhostSleepStepResult",
    "GhostSleepStore",
    "SLEEP_SCHEMA_VERSION",
]
