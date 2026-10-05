"""Bounded presenter and action dispatcher for the local context UI."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from codey.ghost import _common
from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.continuity import GhostContinuityItem, GhostContinuityStore
from codey.ghost.hebbian import GhostHebbianStore, GhostNode, node_id_for_candidate
from codey.ghost.inbox import GhostInboxStore, GhostMemoryCandidate
from codey.ghost.observations import MAX_OBSERVATIONS, GhostObservationStore
from codey.ghost.schema import (
    MAX_SIGNAL_QUOTE_CHARS,
    GhostSignal,
    clip_signal_text,
    contains_sensitive_signal_text,
    quote_is_grounded,
)
from codey.ghost.sleep import GhostSleepStore
from codey.ghost.typed_fields import render_typed_field
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.ghost.work_queue_model import GhostWorkItem

CONTROL_SURFACE_SCHEMA_VERSION = 1
MAX_SUMMARY_ITEMS = 20
MAX_CONTEXT_ITEMS = 8
MAX_UI_TEXT_CHARS = 140
MAX_EVIDENCE_PREVIEW_CHARS = 120


class _ResettableStore(Protocol):
    def delete_scope(self, scope: str, *, project: str = "", session_id: str = "") -> object: ...
    def reset_all(self, **kwargs: object) -> object: ...

_ACTIONS = frozenset({
    "propose_preference",
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
    sleep: GhostSleepStore | None = None
    work_queue: GhostWorkQueueStore | None = None
    affinity: GhostAffinityStore | None = None
    observations: GhostObservationStore | None = None

    @classmethod
    def from_state_home(cls, state_home: str | Path | None) -> GhostControlSurface:
        if not state_home:
            return cls()
        return cls(
            inbox=GhostInboxStore(state_home),
            hebbian=GhostHebbianStore(state_home),
            continuity=GhostContinuityStore(state_home),
            sleep=GhostSleepStore(state_home),
            work_queue=GhostWorkQueueStore(state_home),
            affinity=GhostAffinityStore(state_home),
            observations=GhostObservationStore(state_home),
        )

    @property
    def available(self) -> bool:
        return self.inbox is not None

    def summary(self, *, session_id: str = "", project: str = "") -> dict[str, object]:
        if not self.available:
            return _unavailable_payload()
        inbox = self.inbox
        assert inbox is not None
        session_ref = clip_signal_text(session_id, 120)
        project_ref = _common.normalize_project(project)
        warnings: list[str] = []

        pending = _safe_rows(
            lambda: inbox.applicable_candidates(
                status="candidate",
                session_id=session_ref,
                project=project_ref,
            ),
            warnings,
            "candidate_read_failed",
        )
        all_nodes = _safe_rows(
            lambda: self.hebbian.list_nodes() if self.hebbian is not None else (),
            warnings,
            "active_context_read_failed",
        )
        active_nodes = _applicable_nodes(all_nodes, session_id=session_ref, project=project_ref)
        accepted = _safe_rows(
            lambda: inbox.applicable_candidates(
                status="accepted", session_id=session_ref, project=project_ref,
            ) if self.hebbian is not None else (),
            warnings,
            "candidate_read_failed",
        )
        repair = (
            tuple(candidate for candidate in accepted if _candidate_needs_repair(candidate, all_nodes))
            if "active_context_read_failed" not in warnings else ()
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
            default=False,
        )

        observation_rows = _safe_rows(
            lambda: self.observations.read_committed(
                session_id=session_ref, project=project_ref, limit=MAX_SUMMARY_ITEMS,
            )
            if self.observations is not None and session_ref
            else (),
            warnings,
            "observation_read_failed",
        )
        affinity_warnings = affinity_health.get("warnings")
        warning_rows = _ui_warnings(
            (*warnings, *(affinity_warnings if isinstance(affinity_warnings, (list, tuple)) else ()))
        )
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
                "repair": len(repair),
                "active": len(active_nodes),
                "continuity": len(continuity_items),
                "tasks": len(work_items),
                "warnings": len(warning_rows),
                "observations": len(observation_rows),
            },
            "context": [_continuity_payload(item) for item in continuity_items[:MAX_CONTEXT_ITEMS]],
            "review": [_candidate_payload(row) for row in pending[:MAX_SUMMARY_ITEMS]],
            "repair": [_candidate_payload(row) for row in repair[:MAX_SUMMARY_ITEMS]],
            "active": [_node_payload(node, project=project_ref, session_id=session_ref) for node in active_nodes[:MAX_SUMMARY_ITEMS]],
            "tasks": [_work_item_payload(item) for item in work_items[:MAX_SUMMARY_ITEMS]],
            "observations": [_observation_payload(row) for row in observation_rows[:MAX_SUMMARY_ITEMS]],
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
            if action == "propose_preference":
                return self._propose_preference(body)
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
            "generated_at": _common.now_iso_z(),
        }
        errors: list[str] = []

        def _export(name: str, load: Any) -> object:
            try:
                return load()
            except Exception as exc:  # noqa: BLE001 - export must report, not raise
                errors.append(f"{name}_export_failed: {type(exc).__name__}")
                return {"error": f"{name}_export_failed"}

        payload["inbox"] = _export("inbox", lambda: self.inbox.export_state()) if self.inbox is not None else {}
        payload["hebbian"] = _export("hebbian", lambda: self.hebbian.export_state()) if self.hebbian is not None else {}
        payload["continuity"] = _export("continuity", lambda: self.continuity.export_state()) if self.continuity is not None else {}
        payload["sleep"] = _export("sleep", lambda: self.sleep.export_state()) if self.sleep is not None else {}
        payload["work_queue"] = _export("work_queue", lambda: self.work_queue.export_state()) if self.work_queue is not None else {}
        payload["affinity"] = _export("affinity", lambda: self.affinity.export_state()) if self.affinity is not None else {}
        payload["observations"] = _export("observations", lambda: self.observations.export_state()) if self.observations is not None else {}
        if errors:
            payload["ok"] = False
            payload["errors"] = errors
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
        candidate = self.inbox.review_candidate(
            candidate_id,
            review_action,
            reviewed_by=clip_signal_text(body.get("reviewed_by") or "ui", 80) or "ui",
        )
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
            if not result.applied and result.reason != "duplicate_evidence":
                payload["ok"] = False
                payload["error"] = "preference could not be activated; retry from Needs attention"
                return 500, payload
        else:
            payload["state_removed"] = self.hebbian.remove_candidate(candidate)
        return 200, payload

    def _propose_preference(self, body: Mapping[str, object]) -> tuple[int, dict[str, object]]:
        if self.inbox is None or self.observations is None:
            return 200, _unavailable_payload()
        if not self.inbox.learning_enabled():
            return 409, _error_payload("local updates are disabled")

        run_id = str(body.get("id") or "").strip()
        session_id = str(body.get("session_id") or "").strip()
        project = _common.normalize_project(body.get("project"))
        scope = str(body.get("scope") or "").strip()
        quote = str(body.get("evidence_quote") or "").strip()
        conflict_key = str(body.get("conflict_key") or "").strip()
        value_key = str(body.get("value_key") or "").strip()
        if not run_id or len(run_id) > 120 or not session_id or len(session_id) > 120:
            return 400, _error_payload("id and session_id required")
        if scope not in {"user", "project", "session"} or (scope == "project" and not project):
            return 400, _error_payload("valid scope required")
        if not quote or len(quote) > MAX_SIGNAL_QUOTE_CHARS:
            return 400, _error_payload("short evidence quote required")
        summary = render_typed_field("style_preference", conflict_key, value_key)
        if not summary:
            return 400, _error_payload("unsupported preference")
        if contains_sensitive_signal_text(quote):
            return 400, _error_payload("sensitive quote rejected")

        rows = self.observations.read_committed(
            session_id=session_id, project=project, limit=MAX_OBSERVATIONS,
        )
        source = next((row for row in rows if row.get("run_id") == run_id
                       and row.get("session_id") == session_id
                       and _common.normalize_project(row.get("project")) == project), None)
        if source is None:
            return 404, _error_payload("committed experience not found")
        user_text = str(source.get("user_text") or "")
        if not quote_is_grounded(quote, user_text):
            return 400, _error_payload("quote is not in the saved request")

        scope_ref = session_id if scope == "session" else project if scope == "project" else ""
        if any(row.scope == scope and row.scope_ref == scope_ref
               and row.conflict_key == f"style_preference:{conflict_key}" and row.value_key == value_key
               and row.status == "accepted" for row in self.inbox.list_candidates()):
            return 409, _error_payload("preference has already been reviewed")

        signal = GhostSignal(
            kind="style_preference", scope=scope, summary=summary,
            evidence_quote=quote, confidence=1.0, source="manual",
            metadata={"conflict_key": conflict_key, "value_key": value_key},
        )
        created = self.inbox.ingest_signals(
            (signal,),
            session_id=session_id, run_id=run_id, project=project, user_text=user_text,
        )
        if not created:
            return 500, _error_payload("preference could not be saved")
        return 200, {
            "schema_version": CONTROL_SURFACE_SCHEMA_VERSION,
            "ok": True,
            "action": "propose_preference",
            "candidate": _candidate_payload(created[0]),
        }

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
        project = _common.normalize_project(body.get("project"))
        session_id = clip_signal_text(body.get("session_id"), 120)
        if scope == "project" and not project:
            return 400, _error_payload("project required")
        if scope == "session" and not session_id:
            return 400, _error_payload("session_id required")
        results, errors = self._mutate_all_stores(
            "delete_scope",
            lambda _name, store: store.delete_scope(scope, project=project, session_id=session_id),
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
            lambda name, store: store.reset_all(preserve_settings=True)
            if name == "inbox"
            else store.reset_all(),
        )
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
        mutate: Callable[[str, _ResettableStore], object],
    ) -> tuple[dict[str, object], list[str]]:
        stores: list[tuple[str, object | None]] = [
            ("inbox", self.inbox),
            ("hebbian", self.hebbian),
            ("continuity", self.continuity),
            ("sleep", self.sleep),
            ("work_queue", self.work_queue),
            ("affinity", self.affinity),
            ("observations", self.observations),
        ]
        results: dict[str, object] = {}
        errors: list[str] = []
        for name, store in stores:
            if store is None:
                results[name] = "unavailable"
                continue
            try:
                results[name] = mutate(name, cast(_ResettableStore, store))
            except (OSError, TypeError, ValueError):
                results[name] = False
                errors.append(f"{name}_{action_name}_failed")
        return results, errors


def _applicable_nodes(
    rows: Iterable[GhostNode],
    *,
    session_id: str,
    project: str,
) -> tuple[GhostNode, ...]:
    out = [
        node for node in rows
        if node.status == "active" and (node.scope == "user"
        or (node.scope == "project" and bool(project) and node.scope_ref == project)
        or (node.scope == "session" and bool(session_id) and node.scope_ref == session_id))
    ]
    return tuple(out)


def _candidate_needs_repair(candidate: GhostMemoryCandidate, nodes: Iterable[GhostNode]) -> bool:
    node_id = node_id_for_candidate(candidate)
    node = next((item for item in nodes if item.id == node_id), None)
    return node is None or candidate.id not in node.candidate_ids


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
    return tuple(_bounded_unique(rows))


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
    project = _common.normalize_project(body.get("project"))
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
    return _common.find_work_item_by_id(items, item_id)


def _work_item_visible_for_scope(
    store: GhostWorkQueueStore,
    item: GhostWorkItem,
    body: Mapping[str, object],
) -> bool:
    session_id = clip_signal_text(body.get("session_id"), 120)
    project = _common.normalize_project(body.get("project"))
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


def _observation_payload(row: object) -> dict[str, object]:
    item = row if isinstance(row, dict) else {}
    user_text = _safe_text(item.get("user_text"), MAX_EVIDENCE_PREVIEW_CHARS)
    return {
        "id": _safe_text(item.get("run_id"), 120),
        "type": "observation",
        "summary": user_text,
        "kind": _safe_text(item.get("mode"), 40),
        "scope": "session",
        "scope_label": _scope_label("session"),
        "status": "committed" if item.get("committed") is True else "recorded",
        "updated_at": _safe_text(item.get("ts"), 80),
    }


def _safe_rows(
    load: Callable[[], Iterable[object]],
    warnings: list[str],
    warning: str,
) -> tuple[Any, ...]:
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
    if isinstance(value, bool):
        return "Unknown confidence"
    try:
        confidence = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return "Unknown confidence"
    if not math.isfinite(confidence):
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
