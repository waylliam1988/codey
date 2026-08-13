"""Bounded presenter and action dispatcher for the local context UI."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.continuity import GhostContinuityItem, GhostContinuityStore
from codey.ghost.hebbian import GhostHebbianStore, GhostNode
from codey.ghost.inbox import GhostInboxStore, GhostMemoryCandidate
from codey.ghost.router import GhostRouteStore
from codey.ghost.schema import clip_signal_text, contains_sensitive_signal_text
from codey.ghost.sleep import GhostSleepStore
from codey.ghost.store import GhostSignalStore
from codey.ghost.work_queue import GhostWorkItem, GhostWorkQueueStore


CONTROL_SURFACE_SCHEMA_VERSION = 1
MAX_SUMMARY_ITEMS = 20
MAX_CONTEXT_ITEMS = 8
MAX_UI_TEXT_CHARS = 140
MAX_EVIDENCE_PREVIEW_CHARS = 120

_ACTIONS = frozenset({
    "accept_candidate",
    "reject_candidate",
    "queue_work_item",
    "reject_work_item",
    "enable_updates",
    "disable_updates",
    "delete_scope",
    "reset_all",
})


@dataclass(frozen=True)
class GhostControlSurface:
    inbox: GhostInboxStore | None = None
    hebbian: GhostHebbianStore | None = None
    continuity: GhostContinuityStore | None = None
    router: GhostRouteStore | None = None
    sleep: GhostSleepStore | None = None
    work_queue: GhostWorkQueueStore | None = None
    affinity: GhostAffinityStore | None = None
    signals: GhostSignalStore | None = None

    @classmethod
    def from_state_home(cls, state_home: str | Path | None) -> "GhostControlSurface":
        if not state_home:
            return cls()
        return cls(
            inbox=GhostInboxStore(state_home),
            hebbian=GhostHebbianStore(state_home),
            continuity=GhostContinuityStore(state_home),
            router=GhostRouteStore(state_home),
            sleep=GhostSleepStore(state_home),
            work_queue=GhostWorkQueueStore(state_home),
            affinity=GhostAffinityStore(state_home),
            signals=GhostSignalStore(state_home),
        )

    @property
    def available(self) -> bool:
        return self.inbox is not None

    def summary(self, *, session_id: str = "", project: str = "") -> dict[str, object]:
        if not self.available:
            return _unavailable_payload()
        session_ref = clip_signal_text(session_id, 120)
        project_ref = _normalize_project(project)
        warnings: list[str] = []

        pending = _safe_rows(
            lambda: self.inbox.applicable_candidates(
                status="candidate",
                session_id=session_ref,
                project=project_ref,
            ),
            warnings,
            "candidate_read_failed",
        )
        active_nodes = _safe_rows(
            lambda: _applicable_nodes(
                self.hebbian,
                session_id=session_ref,
                project=project_ref,
            ),
            warnings,
            "active_context_read_failed",
        )
        continuity_items = _safe_rows(
            lambda: self.continuity.list_items(session_id=session_ref, project=project_ref)
            if self.continuity is not None
            else (),
            warnings,
            "continuity_read_failed",
        )
        work_items = _safe_rows(
            lambda: self.work_queue.list_items(
                status="candidate,queued,running,blocked",
                session_id=session_ref,
                project=project_ref,
            )
            if self.work_queue is not None
            else (),
            warnings,
            "work_item_read_failed",
        )
        affinity_health = _safe_affinity_health(
            self.affinity,
            warnings,
            session_id=session_ref,
            project=project_ref,
        )
        updates_enabled = _safe_bool(
            lambda: bool(self.inbox.learning_enabled()) if self.inbox is not None else False,
            warnings,
            "settings_read_failed",
            default=True,
        )

        warning_rows = _ui_warnings((*warnings, *affinity_health.get("warnings", ())))
        return {
            "schema_version": CONTROL_SURFACE_SCHEMA_VERSION,
            "ok": True,
            "available": True,
            "enabled": updates_enabled,
            "scope": {
                "session_id": session_ref,
                "project": project_ref,
            },
            "counts": {
                "review": len(pending),
                "active": len(active_nodes),
                "continuity": len(continuity_items),
                "tasks": len(work_items),
                "warnings": len(warning_rows),
            },
            "context": [_continuity_payload(item) for item in continuity_items[:MAX_CONTEXT_ITEMS]],
            "review": [_candidate_payload(row) for row in pending[:MAX_SUMMARY_ITEMS]],
            "active": [_node_payload(node, project=project_ref, session_id=session_ref) for node in active_nodes[:MAX_SUMMARY_ITEMS]],
            "tasks": [_work_item_payload(item) for item in work_items[:MAX_SUMMARY_ITEMS]],
            "health": {
                "status": "warning" if warning_rows else "ok",
                "associations": affinity_health.get("status") or "unavailable",
                "association_nodes": affinity_health.get("nodes", 0),
                "association_edges": affinity_health.get("edges", 0),
                "warnings": warning_rows,
            },
        }

    def dispatch_action(self, body: object) -> tuple[int, dict[str, object]]:
        if not isinstance(body, Mapping):
            return 400, _error_payload("invalid request")
        if not self.available:
            return 200, _unavailable_payload()
        action = clip_signal_text(body.get("action"), 80)
        if action not in _ACTIONS:
            return 400, _error_payload("unsupported action")
        try:
            if action == "accept_candidate":
                return self._review_candidate(body, review_action="accept")
            if action == "reject_candidate":
                return self._review_candidate(body, review_action="reject")
            if action == "queue_work_item":
                return self._transition_work_item(body, work_action="queue")
            if action == "reject_work_item":
                return self._transition_work_item(body, work_action="reject")
            if action == "enable_updates":
                return self._set_updates(True)
            if action == "disable_updates":
                return self._set_updates(False)
            if action == "delete_scope":
                return self._delete_scope(body)
            if action == "reset_all":
                return self._reset_all(body)
        except ValueError as exc:
            return 400, _error_payload(exc)
        except (OSError, TypeError) as exc:
            return 500, _error_payload(exc)
        return 400, _error_payload("unsupported action")

    def export_state(self) -> dict[str, object]:
        if not self.available:
            return _unavailable_payload()
        payload: dict[str, object] = {
            "schema_version": CONTROL_SURFACE_SCHEMA_VERSION,
            "ok": True,
            "available": True,
            "generated_at": _now(),
        }
        payload["inbox"] = self.inbox.export_state() if self.inbox is not None else {}
        payload["signals"] = list(self.signals.read_all()) if self.signals is not None else []
        payload["hebbian"] = self.hebbian.export_state() if self.hebbian is not None else {}
        payload["continuity"] = self.continuity.export_state() if self.continuity is not None else {}
        payload["router"] = self.router.export_state() if self.router is not None else {}
        payload["sleep"] = self.sleep.export_state() if self.sleep is not None else {}
        payload["work_queue"] = self.work_queue.export_state() if self.work_queue is not None else {}
        payload["affinity"] = self.affinity.export_state() if self.affinity is not None else {}
        return payload

    def _review_candidate(self, body: Mapping[str, object], *, review_action: str) -> tuple[int, dict[str, object]]:
        if self.inbox is None:
            return 200, _unavailable_payload()
        candidate_id = clip_signal_text(body.get("id") or body.get("candidate_id"), 120)
        if not candidate_id:
            return 400, _error_payload("id required")
        current = _find_candidate(self.inbox.list_candidates(), candidate_id)
        if current is None:
            return 404, _error_payload("candidate not found")
        if not _candidate_visible_for_scope(current, body):
            return 409, _error_payload("scope changed")
        candidate = self.inbox.review_candidate(candidate_id, review_action, reviewed_by="ui")
        if candidate is None:
            return 404, _error_payload("candidate not found")
        payload: dict[str, object] = {
            "schema_version": CONTROL_SURFACE_SCHEMA_VERSION,
            "ok": True,
            "action": f"{review_action}_candidate",
            "candidate": _candidate_payload(candidate),
        }
        if self.hebbian is None:
            return 200, payload
        if review_action == "accept":
            related = [
                row for row in self.inbox.list_candidates(status="accepted")
                if row.id != candidate.id and row.run_id and row.run_id == candidate.run_id
            ]
            result = self.hebbian.reinforce_candidate(candidate, related_candidates=related)
            payload["state_update"] = {
                "applied": result.applied,
                "reason": _safe_text(result.reason, 80),
            }
        else:
            payload["state_removed"] = self.hebbian.remove_candidate(candidate)
        return 200, payload

    def _transition_work_item(self, body: Mapping[str, object], *, work_action: str) -> tuple[int, dict[str, object]]:
        if self.work_queue is None:
            return 200, _unavailable_payload()
        item_id = clip_signal_text(body.get("id") or body.get("item_id"), 120)
        if not item_id:
            return 400, _error_payload("id required")
        item_before = _find_work_item(
            self.work_queue.list_items(status="candidate,queued,running,blocked,rejected"),
            item_id,
        )
        if item_before is None:
            return 404, _error_payload("work item not found")
        if not _work_item_visible_for_scope(self.work_queue, item_before, body):
            return 409, _error_payload("scope changed")
        if work_action == "reject" and item_before.status == "running":
            return 409, _error_payload("running item cannot be rejected here")
        item = (
            self.work_queue.queue_item(item_id)
            if work_action == "queue"
            else self.work_queue.reject_item(item_id)
        )
        if item is None:
            return 404, _error_payload("work item not found")
        return 200, {
            "schema_version": CONTROL_SURFACE_SCHEMA_VERSION,
            "ok": True,
            "action": f"{work_action}_work_item",
            "item": _work_item_payload(item),
        }

    def _set_updates(self, enabled: bool) -> tuple[int, dict[str, object]]:
        if self.inbox is None:
            return 200, _unavailable_payload()
        ok = self.inbox.set_learning_enabled(enabled)
        return 200 if ok else 500, {
            "schema_version": CONTROL_SURFACE_SCHEMA_VERSION,
            "ok": bool(ok),
            "enabled": bool(self.inbox.learning_enabled()),
        }

    def _delete_scope(self, body: Mapping[str, object]) -> tuple[int, dict[str, object]]:
        if body.get("confirm") is not True:
            return 400, _error_payload("confirm required")
        scope = clip_signal_text(body.get("scope"), 40).lower()
        if scope not in {"user", "project", "session"}:
            return 400, _error_payload("scope must be user, project, or session")
        project = _normalize_project(body.get("project"))
        session_id = clip_signal_text(body.get("session_id"), 120)
        if scope == "project" and not project:
            return 400, _error_payload("project required")
        if scope == "session" and not session_id:
            return 400, _error_payload("session_id required")
        results, errors = self._mutate_all_stores(
            "delete_scope",
            lambda store: store.delete_scope(scope, project=project, session_id=session_id),
            include_signals=True,
        )
        return (200 if not errors else 500), {
            "schema_version": CONTROL_SURFACE_SCHEMA_VERSION,
            "ok": not errors,
            "action": "delete_scope",
            "scope": scope,
            "results": results,
            "errors": errors,
        }

    def _reset_all(self, body: Mapping[str, object]) -> tuple[int, dict[str, object]]:
        if body.get("confirm") is not True:
            return 400, _error_payload("confirm required")
        results, errors = self._mutate_all_stores(
            "reset_all",
            lambda store: store.reset_all(preserve_settings=True)
            if isinstance(store, GhostInboxStore)
            else store.reset_all(),
            include_signals=False,
        )
        if self.signals is not None:
            try:
                self.signals.delete_all()
                results["signals"] = True
            except OSError:
                results["signals"] = False
                errors.append("signals_reset_failed")
        return (200 if not errors else 500), {
            "schema_version": CONTROL_SURFACE_SCHEMA_VERSION,
            "ok": not errors,
            "action": "reset_all",
            "results": results,
            "errors": errors,
        }

    def _mutate_all_stores(
        self,
        action_name: str,
        mutate: Callable[[object], object],
        *,
        include_signals: bool,
    ) -> tuple[dict[str, object], list[str]]:
        stores: list[tuple[str, object | None]] = [
            ("inbox", self.inbox),
            ("hebbian", self.hebbian),
            ("continuity", self.continuity),
            ("router", self.router),
            ("sleep", self.sleep),
            ("work_queue", self.work_queue),
            ("affinity", self.affinity),
        ]
        if include_signals:
            stores.insert(1, ("signals", self.signals))
        results: dict[str, object] = {}
        errors: list[str] = []
        for name, store in stores:
            if store is None:
                results[name] = "unavailable"
                continue
            try:
                results[name] = mutate(store)
            except (OSError, TypeError, ValueError):
                results[name] = False
                errors.append(f"{name}_{action_name}_failed")
        return results, errors


def _applicable_nodes(
    store: GhostHebbianStore | None,
    *,
    session_id: str,
    project: str,
) -> tuple[GhostNode, ...]:
    if store is None:
        return ()
    rows = store.list_nodes(status="active")
    out = [
        node for node in rows
        if node.scope == "user"
        or (node.scope == "project" and bool(project) and node.scope_ref == project)
        or (node.scope == "session" and bool(session_id) and node.scope_ref == session_id)
    ]
    return tuple(out)


def _safe_affinity_health(
    store: GhostAffinityStore | None,
    warnings: list[str],
    *,
    session_id: str,
    project: str,
) -> dict[str, object]:
    if store is None:
        return {"status": "unavailable", "nodes": 0, "edges": 0, "warnings": []}
    try:
        nodes = store.list_nodes(session_id=session_id, project=project)
        edges = store.list_edges(session_id=session_id, project=project)
    except Exception:
        warnings.append("association_read_failed")
        return {"status": "warning", "nodes": 0, "edges": 0, "warnings": ["association_read_failed"]}
    store_warnings = _ui_warnings(getattr(store, "last_warnings", ()) or ())
    return {
        "status": "warning" if store_warnings else "available",
        "nodes": len(nodes),
        "edges": len(edges),
        "warnings": store_warnings,
    }


def _ui_warnings(warnings: Iterable[object]) -> tuple[str, ...]:
    rows: list[str] = []
    for warning in warnings:
        text = str(warning or "").strip()
        raw = text.lower()
        if not raw:
            continue
        if raw.startswith("some local "):
            rows.append(text)
        elif raw.startswith("association_") or raw.startswith("affinity_"):
            rows.append("Some local ordering could not be read")
        elif "settings" in raw:
            rows.append("Local context settings could not be read")
        else:
            rows.append("Some local context could not be read")
    return _bounded_unique(rows)


def _find_candidate(
    candidates: Iterable[GhostMemoryCandidate],
    candidate_id: str,
) -> GhostMemoryCandidate | None:
    target = clip_signal_text(candidate_id, 120)
    for candidate in candidates:
        if candidate.id == target:
            return candidate
    return None


def _candidate_visible_for_scope(
    candidate: GhostMemoryCandidate,
    body: Mapping[str, object],
) -> bool:
    session_id = clip_signal_text(body.get("session_id"), 120)
    project = _normalize_project(body.get("project"))
    if candidate.scope == "user":
        return True
    if candidate.scope == "project":
        return bool(project and candidate.project == project)
    if candidate.scope == "session":
        return bool(session_id and candidate.session_id == session_id)
    return False


def _find_work_item(
    items: Iterable[GhostWorkItem],
    item_id: str,
) -> GhostWorkItem | None:
    target = clip_signal_text(item_id, 120)
    for item in items:
        if item.id == target:
            return item
    return None


def _work_item_visible_for_scope(
    store: GhostWorkQueueStore,
    item: GhostWorkItem,
    body: Mapping[str, object],
) -> bool:
    session_id = clip_signal_text(body.get("session_id"), 120)
    project = _normalize_project(body.get("project"))
    if item.scope == "user":
        return True
    if not session_id and not project:
        return False
    visible = store.list_items(
        status="candidate,queued,running,blocked,rejected",
        session_id=session_id,
        project=project,
    )
    return _find_work_item(visible, item.id) is not None


def _candidate_payload(candidate: GhostMemoryCandidate) -> dict[str, object]:
    return {
        "id": candidate.id,
        "type": "candidate",
        "summary": _safe_text(candidate.summary),
        "kind": _kind_label(candidate.signal_kind),
        "scope": candidate.scope,
        "scope_label": _scope_label(candidate.scope),
        "status": candidate.status,
        "status_label": _candidate_status_label(candidate.status),
        "confidence": _confidence_label(candidate.confidence),
        "evidence_preview": _safe_evidence_preview(candidate.evidence_quote),
        "updated_at": _safe_text(candidate.updated_at, 80),
    }


def _node_payload(node: GhostNode, *, project: str, session_id: str) -> dict[str, object]:
    return {
        "id": node.id,
        "type": "active",
        "summary": _safe_text(node.label),
        "kind": _kind_label(node.kind),
        "scope": node.scope,
        "scope_label": _scope_label(node.scope),
        "reason": _node_reason(node, project=project, session_id=session_id),
        "updated_at": _safe_text(node.updated_at, 80),
    }


def _continuity_payload(item: GhostContinuityItem) -> dict[str, object]:
    return {
        "id": item.id,
        "type": "context",
        "summary": _safe_text(item.text),
        "kind": _continuity_kind_label(item.kind),
        "scope": item.scope,
        "scope_label": _scope_label(item.scope),
        "updated_at": _safe_text(item.updated_at, 80),
    }


def _work_item_payload(item: GhostWorkItem) -> dict[str, object]:
    return {
        "id": item.id,
        "type": "task",
        "summary": _safe_text(item.title),
        "kind": _work_kind_label(item.kind),
        "scope": item.scope,
        "scope_label": _scope_label(item.scope),
        "status": item.status,
        "status_label": _work_status_label(item.status),
        "updated_at": _safe_text(item.updated_at, 80),
    }


def _safe_rows(
    load: Callable[[], Iterable[object]],
    warnings: list[str],
    warning: str,
) -> tuple:
    try:
        return tuple(load())
    except Exception:
        warnings.append(warning)
        return ()


def _safe_bool(
    load: Callable[[], bool],
    warnings: list[str],
    warning: str,
    *,
    default: bool,
) -> bool:
    try:
        return bool(load())
    except Exception:
        warnings.append(warning)
        return default


def _safe_text(value: object, limit: int = MAX_UI_TEXT_CHARS) -> str:
    text = " ".join(str(value or "").replace("\r\n", "\n").replace("\r", "\n").split())
    text = clip_signal_text(text, limit)
    if not text:
        return ""
    if contains_sensitive_signal_text(text):
        return "Hidden item"
    return text


def _safe_evidence_preview(value: object) -> str:
    text = _safe_text(value, MAX_EVIDENCE_PREVIEW_CHARS)
    if not text or text == "Hidden item":
        return "Evidence hidden"
    return text


def _kind_label(kind: object) -> str:
    return {
        "style_preference": "Preference",
        "correction": "Correction",
        "research_interest": "Research interest",
        "long_term_goal": "Long-term focus",
        "action_tendency": "Work tendency",
    }.get(str(kind or "").strip().lower(), "Context")


def _continuity_kind_label(kind: object) -> str:
    return {
        "recent_focus": "Recent focus",
        "open_question": "Open question",
        "fresh_correction": "Fresh correction",
        "recently_reinforced_preference": "Recently reinforced",
        "long_term_goal": "Long-term focus",
        "active_project": "Current project",
    }.get(str(kind or "").strip().lower(), "Context")


def _work_kind_label(kind: object) -> str:
    return {
        "research": "Research",
        "coding": "Coding",
        "review": "Review",
        "memory_sleep": "Maintenance",
        "open_question": "Open question",
        "project_followup": "Project follow-up",
    }.get(str(kind or "").strip().lower(), "Task")


def _scope_label(scope: object) -> str:
    return {
        "user": "This device",
        "project": "Current project",
        "session": "Current chat",
    }.get(str(scope or "").strip().lower(), "Local")


def _candidate_status_label(status: object) -> str:
    return {
        "candidate": "Pending review",
        "accepted": "Accepted",
        "rejected": "Rejected",
        "superseded": "Replaced",
    }.get(str(status or "").strip().lower(), "Pending review")


def _work_status_label(status: object) -> str:
    return {
        "candidate": "Pending",
        "queued": "Queued",
        "running": "In progress",
        "blocked": "Blocked",
        "done": "Done",
        "rejected": "Rejected",
        "expired": "Expired",
    }.get(str(status or "").strip().lower(), "Pending")


def _confidence_label(value: object) -> str:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return "Unknown confidence"
    if confidence >= 0.85:
        return "High confidence"
    if confidence >= 0.65:
        return "Medium confidence"
    return "Low confidence"


def _node_reason(node: GhostNode, *, project: str, session_id: str) -> str:
    if node.scope == "session" and session_id and node.scope_ref == session_id:
        return "Current chat"
    if node.scope == "project" and project and node.scope_ref == project:
        return "Current project"
    return "Accepted preference"


def _bounded_unique(values: Iterable[object]) -> list[str]:
    out: list[str] = []
    for value in values:
        text = _safe_text(value, 120)
        if text and text not in out:
            out.append(text)
        if len(out) >= 12:
            break
    return out


def _normalize_project(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return clip_signal_text(Path(text).expanduser().resolve(), 240)
    except (OSError, RuntimeError, ValueError):
        return clip_signal_text(text, 240)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _unavailable_payload() -> dict[str, object]:
    return {
        "schema_version": CONTROL_SURFACE_SCHEMA_VERSION,
        "ok": False,
        "available": False,
        "reason": "unavailable",
    }


def _error_payload(error: object) -> dict[str, object]:
    return {
        "schema_version": CONTROL_SURFACE_SCHEMA_VERSION,
        "ok": False,
        "error": _safe_text(error, 160) or "error",
    }


__all__ = [
    "CONTROL_SURFACE_SCHEMA_VERSION",
    "GhostControlSurface",
]
