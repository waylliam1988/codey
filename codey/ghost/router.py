"""Bounded local audit and policy for automatic task routing.

The router is deliberately narrow: it can choose an execution mode, but it
cannot grant permissions, choose tool arguments, inspect project files, or
change the current request. Provider/browser attachment is injected by the
caller so this module stays storage-and-policy only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Callable, Iterable, Protocol
import uuid

from codey.runtime import cancellation
from codey.ghost.numbers import clamp_unit_float
from codey.ghost.schema import clip_signal_text
from codey.storage.file_lock import reset_event_backed_state, with_file_lock
from codey.storage.local_store import DEFAULT_STATE_HOME, project_key, session_key, write_json_atomic


ROUTER_SCHEMA_VERSION = 1
MAX_ROUTER_TASK_CHARS = 900
MAX_ROUTER_REASON_CHARS = 240
MAX_ROUTER_DIAGNOSTICS = 8
MAX_ROUTER_EVENTS = 5_000
MAX_ROUTER_RECORDS = 200
MAX_ROUTER_STATE_BYTES = 512 * 1024
MAX_ROUTER_EVENTS_BYTES = 1024 * 1024
DEFAULT_GHOST_ROUTER_TIMEOUT = 25.0
DEFAULT_GHOST_ROUTER_NEW_CHAT_TIMEOUT = 12.0
DEFAULT_GHOST_ROUTER_ATTEMPTS = 2
ACCEPT_CONFIDENCE = 0.70
WRITE_UPGRADE_CONFIDENCE = 0.85
_STATE_KIND = "ghost_router_state_projection"

ROUTER_MODES = (
    "chat",
    "planning_readonly",
    "research",
    "project",
    "hybrid",
    "review",
)
ROUTER_JSON_MODES = (*ROUTER_MODES, "project_writer")
MODE_ALIASES = {
    "planning": "planning_readonly",
    "readonly": "planning_readonly",
    "read_only": "planning_readonly",
    "project_writer": "project",
    "writer": "project",
    "coding": "project",
    "code": "project",
}
_WRITE_MODES = {"project", "hybrid"}
_NO_EDIT_RE = re.compile(
    r"不要(?:改|修改|动|写|编辑|保存)|先别(?:改|修改|动|写|编辑|保存)|"
    r"别(?:改|修改|动|写|编辑|保存)|不(?:要)?(?:改|写|编辑)(?:代码|文件)?|"
    r"只(?:给|要)?方案|只读|只看|"
    r"\b(?:do\s+not|don't|dont)\s+(?:edit|modify|change|write)\b|"
    r"\b(?:no\s+changes?|without\s+editing|read[-\s]?only|plan\s+only)\b",
    re.IGNORECASE,
)
_PROJECT_ACCESS_DENIAL_MARKERS = (
    "不要",
    "不用",
    "无需",
    "不需要",
    "不",
    "别",
    "do not",
    "don't",
    "dont",
    "without",
    "no",
    "not",
)
_PROJECT_ACCESS_ACTION_MARKERS = (
    "读写",
    "读取",
    "读",
    "看",
    "查看",
    "打开",
    "访问",
    "read",
    "reading",
    "access",
    "accessing",
    "open",
    "opening",
    "inspect",
    "inspecting",
    "look at",
)
_PROJECT_ACCESS_OBJECT_MARKERS = (
    "项目文件",
    "工程文件",
    "项目代码",
    "工程代码",
    "文件",
    "代码",
    "源码",
    "project files",
    "project file",
    "project code",
    "files",
    "file",
    "code",
    "source",
)
_CHAT_ONLY_MARKERS = ("只聊天", "普通聊天", "chat only")
_PROJECT_ACCESS_DENIAL_WINDOW = 32
_ASCII_WORD_RE = re.compile(r"[a-z0-9_'-]", re.IGNORECASE)
_RESEARCH_RE = re.compile(
    r"查一下|调研|搜索|搜一下|最新|今天|联网|资料|"
    r"\b(?:latest|today|current|research|search|browse|web|look\s+up)\b",
    re.IGNORECASE,
)


class RouteProvider(Protocol):
    def new_chat(self, timeout: float | None = None) -> None:
        """Start an isolated router chat."""

    def send(self, text: str, timeout: float | None = None) -> str:
        """Send one routing prompt and return a reply."""

    def close(self) -> None:
        """Close the temporary provider."""


@dataclass(frozen=True)
class GhostRouteRequest:
    task: str
    baseline_mode: str
    run_id: str = ""
    session_id: str = ""
    project: str = ""
    provider_id: str = ""
    continue_request: bool = False
    has_reviewable_diff: bool = False


@dataclass(frozen=True)
class GhostRouteDecision:
    mode: str
    confidence: float
    reason: str
    parse_ok: bool
    diagnostics: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "confidence": self.confidence,
            "reason": self.reason,
            "parse_ok": self.parse_ok,
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True)
class GhostRouteResult:
    ok: bool
    baseline_mode: str
    selected_mode: str
    final_mode: str
    confidence: float = 0.0
    accepted: bool = False
    reason: str = ""
    skipped_reason: str = ""
    diagnostics: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    provider_id: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "baseline_mode": self.baseline_mode,
            "selected_mode": self.selected_mode,
            "final_mode": self.final_mode,
            "confidence": self.confidence,
            "accepted": self.accepted,
            "reason": self.reason,
            "skipped_reason": self.skipped_reason,
            "diagnostics": list(self.diagnostics),
            "warnings": list(self.warnings),
            "provider_id": self.provider_id,
        }


class GhostRouteStore:
    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.directory = Path(state_home) / "ghost"
        self.state_path = self.directory / "router_state.json"
        self.events_path = self.directory / "router_events.jsonl"
        self.last_warnings: tuple[str, ...] = ()
        self._events_read_blocked = False

    def append_result(
        self,
        result: GhostRouteResult,
        request: GhostRouteRequest,
    ) -> bool:
        event = _route_event(result, request)
        try:
            with with_file_lock(self.events_path):
                records = self._load_records_for_event_rewrite()
                if self._events_read_blocked:
                    return False
                if records and not self.events_path.exists():
                    self._rewrite_events(records)
                records = _bounded_records((*records, _record_from_event(event)))
                self.directory.mkdir(parents=True, exist_ok=True)
                with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(_json_line(event))
                warnings: list[str] = []
                try:
                    write_json_atomic(
                        self.state_path,
                        {
                            "schema_version": ROUTER_SCHEMA_VERSION,
                            "kind": _STATE_KIND,
                            "updated_at": _now(),
                            "records": [record for record in records],
                            "warnings": [],
                        },
                        max_bytes=MAX_ROUTER_STATE_BYTES,
                    )
                except (OSError, TypeError, ValueError):
                    warnings.append("router_state_write_failed")
                try:
                    self._compact_if_needed(records)
                    warnings.extend(self.last_warnings)
                except (OSError, TypeError, ValueError):
                    warnings.append("router_compaction_failed")
                self.last_warnings = tuple(dict.fromkeys(warnings))
                return True
        except (OSError, TypeError, ValueError):
            self.last_warnings = ("router_audit_write_failed",)
            return False

    def export_state(self) -> dict[str, object]:
        events_missing = not self.events_path.is_file()
        events = self._read_events()
        event_warnings = self.last_warnings
        event_records = _bounded_records(_record_from_event(event) for event in events)
        if self._events_read_blocked:
            records = tuple(self._load_projection_records())
        elif events_missing:
            records = tuple(self._load_projection_records())
            if records:
                event_warnings = ("router_events_missing",)
        else:
            records = event_records
        return {
            "schema_version": ROUTER_SCHEMA_VERSION,
            "router": {
                "schema_version": ROUTER_SCHEMA_VERSION,
                "kind": _STATE_KIND,
                "records": list(records),
                "warnings": list(event_warnings),
            },
            "router_events": events,
            "warnings": list(event_warnings),
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
    ) -> int:
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope not in {"user", "project", "session"}:
            raise ValueError("scope must be user, project, or session")
        project_ref = _project_ref(project)
        session_ref = _session_ref(session_id)
        if normalized_scope == "project" and not project_ref:
            raise ValueError("project is required for project scope deletion")
        if normalized_scope == "session" and not session_ref:
            raise ValueError("session_id is required for session scope deletion")
        with with_file_lock(self.events_path):
            records = list(self._load_records_for_event_rewrite())
            if self._events_read_blocked:
                warning = self.last_warnings[0] if self.last_warnings else "router_events_unreadable"
                raise OSError(warning)
            kept = [
                record for record in records
                if not _record_scope_matches(
                    record,
                    normalized_scope,
                    project_ref=project_ref,
                    session_ref=session_ref,
                )
            ]
            removed = len(records) - len(kept)
            if not removed:
                return 0
            control = _control_event(
                "ghost_router_scope_deleted",
                {
                    "scope": normalized_scope,
                    "project_ref": project_ref if normalized_scope == "project" else "",
                    "session_ref": session_ref if normalized_scope == "session" else "",
                    "removed_count": removed,
                },
            )
            self._rewrite_events(kept, control_event=control)
            write_json_atomic(
                self.state_path,
                {
                    "schema_version": ROUTER_SCHEMA_VERSION,
                    "kind": _STATE_KIND,
                    "updated_at": _now(),
                    "records": kept,
                    "warnings": [],
                },
                max_bytes=MAX_ROUTER_STATE_BYTES,
            )
            return removed

    def compact_if_needed(self) -> dict[str, object]:
        before = _event_file_stats(self.events_path, max_bytes=MAX_ROUTER_EVENTS_BYTES)
        if not before["readable"]:
            warning = str(before["warning"] or "router_events_unreadable")
            self.last_warnings = (warning,)
            return _compact_payload(False, False, before, before, (warning,))
        if before["events"] <= MAX_ROUTER_EVENTS and before["bytes"] <= MAX_ROUTER_EVENTS_BYTES:
            return _compact_payload(True, False, before, before, self.last_warnings)
        with with_file_lock(self.events_path):
            records = self._load_records_for_event_rewrite()
            if self._events_read_blocked:
                return _compact_payload(False, False, before, before, self.last_warnings)
            self._rewrite_events(records)
        after = _event_file_stats(self.events_path, max_bytes=MAX_ROUTER_EVENTS_BYTES)
        return _compact_payload(True, after != before, before, after, self.last_warnings)

    def _load_records(self) -> tuple[dict[str, object], ...]:
        projection_records = self._load_projection_records()
        if projection_records:
            return projection_records
        return self._load_records_from_events()

    def _load_records_for_event_rewrite(self) -> tuple[dict[str, object], ...]:
        if self.events_path.exists():
            return self._load_records_from_events()
        self._events_read_blocked = False
        self.last_warnings = ()
        return self._load_projection_records()

    def _load_projection_records(self) -> tuple[dict[str, object], ...]:
        payload = _read_json_dict(self.state_path, max_bytes=MAX_ROUTER_STATE_BYTES)
        if payload and payload.get("schema_version") == ROUTER_SCHEMA_VERSION:
            records = tuple(_clean_record(row) for row in _list(payload.get("records")))
            rows = tuple(row for row in records if row)
            if rows:
                return rows
        return ()

    def _load_records_from_events(self) -> tuple[dict[str, object], ...]:
        events = self._read_events()
        if self._events_read_blocked:
            return ()
        return _bounded_records(_record_from_event(event) for event in events)

    def _read_events(self) -> list[dict[str, object]]:
        self._events_read_blocked = False
        try:
            if not self.events_path.is_file():
                self.last_warnings = ()
                return []
            if self.events_path.stat().st_size > MAX_ROUTER_EVENTS_BYTES:
                self.last_warnings = ("router_events_too_large",)
                self._events_read_blocked = True
                return []
            lines = self.events_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            self.last_warnings = ("router_events_unreadable",)
            self._events_read_blocked = True
            return []
        rows: list[dict[str, object]] = []
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("schema_version") == ROUTER_SCHEMA_VERSION:
                rows.append(payload)
        self.last_warnings = ()
        return rows

    def _rewrite_events(
        self,
        records: Iterable[dict[str, object]],
        *,
        control_event: dict[str, object] | None = None,
    ) -> None:
        rows = [_event_from_record(record) for record in _bounded_records(records)]
        if control_event is not None:
            rows.append(control_event)
        else:
            rows.append(_control_event("ghost_router_events_compacted", {"records": len(rows)}))
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.events_path.with_name(f".{self.events_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                for row in rows:
                    handle.write(_json_line(row).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.events_path)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def _compact_if_needed(self, records: Iterable[dict[str, object]]) -> None:
        stats = _event_file_stats(self.events_path, max_bytes=MAX_ROUTER_EVENTS_BYTES)
        if not stats["readable"]:
            self.last_warnings = (str(stats["warning"] or "router_events_unreadable"),)
            return
        if stats["events"] <= MAX_ROUTER_EVENTS and stats["bytes"] <= MAX_ROUTER_EVENTS_BYTES:
            return
        self._rewrite_events(records)


class GhostRouter:
    def __init__(self, store: GhostRouteStore | None = None) -> None:
        self.store = store

    def route(
        self,
        request: GhostRouteRequest,
        *,
        provider_factory: Callable[[str], RouteProvider] | None,
        timeout: float = DEFAULT_GHOST_ROUTER_TIMEOUT,
        new_chat_timeout: float = DEFAULT_GHOST_ROUTER_NEW_CHAT_TIMEOUT,
        max_attempts: int = DEFAULT_GHOST_ROUTER_ATTEMPTS,
        start_new_chat: bool = True,
    ) -> GhostRouteResult:
        baseline = normalize_route_mode(request.baseline_mode) or "chat"
        request = _request_with_baseline(request, baseline)
        if provider_factory is None:
            return self._finish(
                GhostRouteResult(
                    True,
                    baseline,
                    "",
                    baseline,
                    skipped_reason="provider_factory_missing",
                    provider_id=clip_signal_text(request.provider_id, 80),
                ),
                request,
            )
        if not str(request.task or "").strip():
            return self._finish(
                GhostRouteResult(
                    True,
                    baseline,
                    "",
                    baseline,
                    skipped_reason="empty_task",
                    provider_id=clip_signal_text(request.provider_id, 80),
                ),
                request,
            )
        diagnostics: list[str] = []
        attempts = max(1, int(max_attempts or 1))
        for _attempt in range(attempts):
            provider: RouteProvider | None = None
            stage = "provider_connect"
            try:
                provider = provider_factory(request.provider_id)
                if start_new_chat:
                    stage = "provider_new_chat"
                    _start_provider_chat(provider, timeout=new_chat_timeout)
                prompt = render_route_prompt(request)
                stage = "provider_send"
                reply = provider.send(prompt, timeout=timeout)
                decision = parse_route_reply(reply)
                return self._finish(finalize_route_decision(request, decision), request)
            except cancellation.TaskCancelled:
                raise
            except Exception as exc:
                if _is_cancel_signal(exc):
                    raise
                diagnostics.append(_diagnostic_code(stage, exc))
            finally:
                _close_provider(provider)
        return self._finish(
            GhostRouteResult(
                False,
                baseline,
                "",
                baseline,
                skipped_reason="router_error",
                diagnostics=tuple(diagnostics[-MAX_ROUTER_DIAGNOSTICS:]),
                provider_id=clip_signal_text(request.provider_id, 80),
            ),
            request,
        )

    def _finish(
        self,
        result: GhostRouteResult,
        request: GhostRouteRequest,
    ) -> GhostRouteResult:
        if self.store is None:
            return result
        audit_ok = self.store.append_result(result, request)
        if audit_ok:
            if self.store.last_warnings:
                return GhostRouteResult(
                    result.ok,
                    result.baseline_mode,
                    result.selected_mode,
                    result.final_mode,
                    confidence=result.confidence,
                    accepted=result.accepted,
                    reason=result.reason,
                    skipped_reason=result.skipped_reason,
                    diagnostics=result.diagnostics,
                    warnings=(*result.warnings, *self.store.last_warnings),
                    provider_id=result.provider_id,
                )
            return result
        warnings = (*result.warnings, *self.store.last_warnings)
        if not warnings:
            warnings = ("router_audit_write_failed",)
        return GhostRouteResult(
            False,
            result.baseline_mode,
            result.selected_mode,
            result.baseline_mode,
            confidence=result.confidence,
            accepted=False,
            reason="",
            skipped_reason="router_audit_failed",
            diagnostics=result.diagnostics,
            warnings=warnings,
            provider_id=result.provider_id,
        )


def render_route_prompt(request: GhostRouteRequest) -> str:
    project = "yes" if str(request.project or "").strip() else "no"
    diff = "yes" if request.has_reviewable_diff else "no"
    continuation = "yes" if request.continue_request else "no"
    task = clip_signal_text(request.task, MAX_ROUTER_TASK_CHARS)
    return (
        "Choose one execution mode for this request.\n"
        "Return exactly one JSON object. Do not answer the request.\n\n"
        "Modes:\n"
        "- chat: answer normally; no project inspection, web research, or file changes.\n"
        "- planning_readonly: inspect or reason about an attached project without changing files.\n"
        "- research: gather fresh external information without changing local files.\n"
        "- project_writer: edit attached project files or run local project checks.\n"
        "- hybrid: gather fresh external information, then update the attached project.\n"
        "- review: review an existing local diff and report findings without editing.\n\n"
        "Routing is not permission. It cannot approve shell commands, grant tools, "
        "or override the user's manual mode.\n"
        "If the user says not to edit, do not choose project_writer or hybrid.\n"
        "If there is no attached project, do not choose project_writer, hybrid, or review.\n"
        "If there is no reviewable diff, do not choose review.\n\n"
        "Return schema:\n"
        '{"mode":"chat|planning_readonly|research|project_writer|hybrid|review",'
        '"confidence":0.0,"reason":"short reason"}\n\n'
        f"Project attached: {project}\n"
        f"Reviewable diff present: {diff}\n"
        f"Continuation request: {continuation}\n"
        "User request excerpt:\n"
        f"{task}\n"
    )


def parse_route_reply(reply: str) -> GhostRouteDecision:
    text = _strip_json_fence(str(reply or ""))
    objects = _json_objects(text)
    if not objects:
        return GhostRouteDecision("", 0.0, "", False, ("no_json_object",))
    if len(objects) > 1:
        return GhostRouteDecision("", 0.0, "", False, ("too_many_json_objects",))
    if text.strip() != objects[0].strip():
        return GhostRouteDecision("", 0.0, "", False, ("json_not_top_level_object",))
    try:
        payload = json.loads(objects[0])
    except json.JSONDecodeError as exc:
        return GhostRouteDecision("", 0.0, "", False, (f"json_error:{exc.msg}",))
    if not isinstance(payload, dict):
        return GhostRouteDecision("", 0.0, "", False, ("json_not_object",))
    mode = normalize_route_mode(payload.get("mode"))
    diagnostics: list[str] = []
    if mode not in ROUTER_MODES:
        diagnostics.append("unknown_mode")
        mode = ""
    return GhostRouteDecision(
        mode=mode,
        confidence=_float_confidence(payload.get("confidence")),
        reason=clip_signal_text(payload.get("reason") or "", MAX_ROUTER_REASON_CHARS),
        parse_ok=not diagnostics,
        diagnostics=tuple(diagnostics),
    )


def finalize_route_decision(
    request: GhostRouteRequest,
    decision: GhostRouteDecision,
) -> GhostRouteResult:
    baseline = normalize_route_mode(request.baseline_mode) or "chat"
    provider_id = clip_signal_text(request.provider_id, 80)
    if not decision.parse_ok:
        return GhostRouteResult(
            True,
            baseline,
            "",
            baseline,
            confidence=decision.confidence,
            skipped_reason="parse_failed",
            diagnostics=decision.diagnostics,
            provider_id=provider_id,
        )
    selected = normalize_route_mode(decision.mode)
    if selected not in ROUTER_MODES:
        return GhostRouteResult(
            True,
            baseline,
            "",
            baseline,
            confidence=decision.confidence,
            skipped_reason="unknown_mode",
            diagnostics=("unknown_mode",),
            provider_id=provider_id,
        )
    threshold = _confidence_threshold(baseline, selected)
    if decision.confidence < threshold:
        return GhostRouteResult(
            True,
            baseline,
            selected,
            baseline,
            confidence=decision.confidence,
            skipped_reason="low_confidence",
            reason=decision.reason,
            provider_id=provider_id,
        )
    final, skipped = _apply_local_policy(request, baseline=baseline, selected=selected)
    return GhostRouteResult(
        True,
        baseline,
        selected,
        final,
        confidence=decision.confidence,
        accepted=not skipped and final != baseline,
        reason=decision.reason,
        skipped_reason=skipped,
        diagnostics=decision.diagnostics,
        provider_id=provider_id,
    )


def normalize_route_mode(value: object) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return MODE_ALIASES.get(text, text)


def route_error_cost(expected: str, observed: str) -> int:
    expected = normalize_route_mode(expected)
    observed = normalize_route_mode(observed)
    if expected == observed:
        return 0
    if not observed:
        return 6
    if expected in _WRITE_MODES or observed in _WRITE_MODES:
        return 5
    if expected in {"research", "review"} or observed in {"research", "review"}:
        return 3
    return 1


def _confidence_threshold(baseline: str, selected: str) -> float:
    if selected in _WRITE_MODES and baseline in {"chat", "planning_readonly"}:
        return WRITE_UPGRADE_CONFIDENCE
    if baseline == "research" and selected == "hybrid":
        return WRITE_UPGRADE_CONFIDENCE
    return ACCEPT_CONFIDENCE


def _apply_local_policy(
    request: GhostRouteRequest,
    *,
    baseline: str,
    selected: str,
) -> tuple[str, str]:
    project_attached = bool(str(request.project or "").strip())
    if selected in {
        "project",
        "hybrid",
        "planning_readonly",
        "review",
    } and _forbids_project_access(request.task):
        if selected == "hybrid" and _research_requested(request.task):
            return "research", "project_access_forbidden"
        return "chat", "project_access_forbidden"
    if _forbids_edit(request.task) and selected in _WRITE_MODES:
        if selected == "hybrid" and _research_requested(request.task):
            return "research", "edit_forbidden"
        return ("planning_readonly" if project_attached else "chat"), "edit_forbidden"
    if not project_attached:
        if selected == "hybrid":
            return "research", "no_project"
        if selected in {"project", "review", "planning_readonly"}:
            return "chat", "no_project"
    if selected == "review" and not request.has_reviewable_diff:
        return ("planning_readonly" if project_attached else "chat"), "no_reviewable_diff"
    return selected or baseline, ""


def _forbids_edit(text: str) -> bool:
    return bool(_NO_EDIT_RE.search(str(text or "")))


def _forbids_project_access(text: str) -> bool:
    normalized = _normalize_policy_text(text)
    if _contains_any_marker(normalized, _CHAT_ONLY_MARKERS):
        return True
    return (
        _contains_ordered_markers(
            normalized,
            _PROJECT_ACCESS_DENIAL_MARKERS,
            _PROJECT_ACCESS_ACTION_MARKERS,
            _PROJECT_ACCESS_OBJECT_MARKERS,
            max_span=_PROJECT_ACCESS_DENIAL_WINDOW,
        )
        or _contains_ordered_markers(
            normalized,
            _PROJECT_ACCESS_DENIAL_MARKERS,
            _PROJECT_ACCESS_OBJECT_MARKERS,
            _PROJECT_ACCESS_ACTION_MARKERS,
            max_span=_PROJECT_ACCESS_DENIAL_WINDOW,
        )
    )


def _normalize_policy_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _contains_ordered_markers(
    text: str,
    first_markers: tuple[str, ...],
    second_markers: tuple[str, ...],
    third_markers: tuple[str, ...],
    *,
    max_span: int,
) -> bool:
    for first in first_markers:
        start = _find_marker(text, first)
        while start >= 0:
            window = text[start:start + max_span]
            for second in second_markers:
                second_index = _find_marker(window, second)
                if second_index < 0:
                    continue
                for third in third_markers:
                    if _find_marker(window, third, second_index + len(second)) >= 0:
                        return True
            start = _find_marker(text, first, start + 1)
    return False


def _contains_any_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(_find_marker(text, marker) >= 0 for marker in markers)


def _find_marker(text: str, marker: str, start: int = 0) -> int:
    if _ascii_marker(marker):
        return _find_ascii_marker(text, marker, start)
    return text.find(marker, start)


def _find_ascii_marker(text: str, marker: str, start: int = 0) -> int:
    cursor = max(0, int(start or 0))
    while True:
        index = text.find(marker, cursor)
        if index < 0:
            return -1
        before = text[index - 1] if index > 0 else ""
        after_index = index + len(marker)
        after = text[after_index] if after_index < len(text) else ""
        if not _ASCII_WORD_RE.match(before) and not _ASCII_WORD_RE.match(after):
            return index
        cursor = index + 1


def _ascii_marker(marker: str) -> bool:
    return marker.isascii()


def _research_requested(text: str) -> bool:
    return bool(_RESEARCH_RE.search(str(text or "")))


def _request_with_baseline(request: GhostRouteRequest, baseline: str) -> GhostRouteRequest:
    return GhostRouteRequest(
        task=request.task,
        baseline_mode=baseline,
        run_id=request.run_id,
        session_id=request.session_id,
        project=request.project,
        provider_id=request.provider_id,
        continue_request=request.continue_request,
        has_reviewable_diff=request.has_reviewable_diff,
    )


def _route_event(
    result: GhostRouteResult,
    request: GhostRouteRequest,
) -> dict[str, object]:
    return {
        "schema_version": ROUTER_SCHEMA_VERSION,
        "kind": "ghost_router_decision",
        "event_id": "gre_" + uuid.uuid4().hex[:24],
        "created_at": _now(),
        "run_id": clip_signal_text(request.run_id, 120),
        "session_id": clip_signal_text(request.session_id, 120),
        "session_ref": _session_ref(request.session_id),
        "project_ref": _project_ref(request.project),
        "provider_id": clip_signal_text(result.provider_id or request.provider_id, 80),
        "baseline_mode": result.baseline_mode,
        "selected_mode": result.selected_mode,
        "final_mode": result.final_mode,
        "confidence": result.confidence,
        "accepted": result.accepted,
        "ok": result.ok,
        "reason": _route_reason_code(result),
        "skipped_reason": clip_signal_text(result.skipped_reason, 80),
        "diagnostics": list(_bounded_diagnostics(result.diagnostics)),
        "warnings": list(_bounded_diagnostics(result.warnings)),
        "task_chars": len(str(request.task or "")),
        "task_hash": _task_hash(request.task),
        "continue_request": bool(request.continue_request),
        "has_reviewable_diff": bool(request.has_reviewable_diff),
    }


def _event_from_record(record: dict[str, object]) -> dict[str, object]:
    payload = dict(record)
    payload["schema_version"] = ROUTER_SCHEMA_VERSION
    payload["kind"] = "ghost_router_decision"
    payload.setdefault("event_id", "gre_" + uuid.uuid4().hex[:24])
    payload.setdefault("created_at", _now())
    return payload


def _control_event(kind: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": ROUTER_SCHEMA_VERSION,
        "kind": kind,
        "event_id": "grc_" + uuid.uuid4().hex[:24],
        "created_at": _now(),
        "payload": payload,
    }


def _record_from_event(event: dict[str, object]) -> dict[str, object]:
    if event.get("kind") != "ghost_router_decision":
        return {}
    return _clean_record(event)


def _clean_record(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    baseline = normalize_route_mode(value.get("baseline_mode"))
    final = normalize_route_mode(value.get("final_mode"))
    selected = normalize_route_mode(value.get("selected_mode"))
    if baseline not in ROUTER_MODES or final not in ROUTER_MODES:
        return {}
    return {
        "created_at": clip_signal_text(value.get("created_at"), 80),
        "run_id": clip_signal_text(value.get("run_id"), 120),
        "session_id": clip_signal_text(value.get("session_id"), 120),
        "session_ref": clip_signal_text(value.get("session_ref"), 80),
        "project_ref": clip_signal_text(value.get("project_ref"), 80),
        "provider_id": clip_signal_text(value.get("provider_id"), 80),
        "baseline_mode": baseline,
        "selected_mode": selected if selected in ROUTER_MODES else "",
        "final_mode": final,
        "confidence": _float_confidence(value.get("confidence")),
        "accepted": bool(value.get("accepted")),
        "ok": bool(value.get("ok", True)),
        "reason": clip_signal_text(value.get("reason"), MAX_ROUTER_REASON_CHARS),
        "skipped_reason": clip_signal_text(value.get("skipped_reason"), 80),
        "diagnostics": list(_bounded_diagnostics(_list(value.get("diagnostics")))),
        "warnings": list(_bounded_diagnostics(_list(value.get("warnings")))),
        "task_chars": _int_or_zero(value.get("task_chars")),
        "task_hash": clip_signal_text(value.get("task_hash"), 80),
        "continue_request": bool(value.get("continue_request")),
        "has_reviewable_diff": bool(value.get("has_reviewable_diff")),
    }


def _bounded_records(records: Iterable[dict[str, object]]) -> tuple[dict[str, object], ...]:
    rows = [record for record in (_clean_record(row) for row in records) if record]
    return tuple(rows[-MAX_ROUTER_RECORDS:])


def _record_scope_matches(
    record: dict[str, object],
    scope: str,
    *,
    project_ref: str,
    session_ref: str,
) -> bool:
    if scope == "user":
        return True
    if scope == "project":
        return bool(project_ref) and str(record.get("project_ref") or "") == project_ref
    if scope == "session":
        return bool(session_ref) and str(record.get("session_ref") or "") == session_ref
    return False


def _project_ref(project: str | Path | None) -> str:
    if not str(project or "").strip():
        return ""
    try:
        return project_key(str(project))
    except (OSError, RuntimeError, ValueError):
        return ""


def _session_ref(session_id: str) -> str:
    text = str(session_id or "").strip()
    if not text:
        return ""
    return session_key(text)


def _task_hash(task: str) -> str:
    return hashlib.sha256(str(task or "").encode("utf-8", errors="replace")).hexdigest()[:24]


def _float_confidence(value: object) -> float:
    return clamp_unit_float(value, digits=4)


def _int_or_zero(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _json_objects(text: str) -> tuple[str, ...]:
    stripped = str(text or "").strip()
    objects: list[str] = []
    start = -1
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(stripped):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                objects.append(stripped[start:index + 1])
                start = -1
    return tuple(objects)


def _strip_json_fence(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 3 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return stripped


def _start_provider_chat(provider: RouteProvider, *, timeout: float) -> None:
    new_chat = getattr(provider, "new_chat", None)
    if callable(new_chat):
        new_chat(timeout=timeout)


def _close_provider(provider: RouteProvider | None) -> None:
    close = getattr(provider, "close", None)
    if callable(close):
        try:
            close()
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            if _is_cancel_signal(exc):
                raise
            pass


def _bounded_diagnostics(items: Iterable[object]) -> tuple[str, ...]:
    out: list[str] = []
    for item in items:
        text = clip_signal_text(item, 200)
        if text:
            out.append(text)
        if len(out) >= MAX_ROUTER_DIAGNOSTICS:
            break
    return tuple(out)


def _diagnostic_code(stage: str, exc: BaseException) -> str:
    safe_stage = str(stage or "provider").strip().lower()
    if safe_stage not in {"provider_connect", "provider_new_chat", "provider_send"}:
        safe_stage = "provider"
    return f"{safe_stage}_failed:{type(exc).__name__}"


def _is_cancel_signal(exc: BaseException | None) -> bool:
    return isinstance(exc, cancellation.TaskCancelled) or type(exc).__name__ == "ControlTeachCancelled"


def _route_reason_code(result: GhostRouteResult) -> str:
    if result.skipped_reason:
        return clip_signal_text(result.skipped_reason, 80)
    if result.accepted:
        return "accepted"
    if result.selected_mode and result.final_mode == result.baseline_mode:
        return "baseline_kept"
    return "baseline"


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
                "warning": "router_events_too_large",
            }
        event_count = len(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return {"events": 0, "bytes": 0, "readable": False, "warning": "router_events_unreadable"}
    return {"events": event_count, "bytes": event_bytes, "readable": True, "warning": ""}


def _compact_payload(
    ok: bool,
    compacted: bool,
    before: dict[str, object],
    after: dict[str, object],
    warnings: Iterable[str],
) -> dict[str, object]:
    return {
        "ok": ok,
        "compacted": compacted,
        "events_before": before["events"],
        "events_after": after["events"],
        "bytes_before": before["bytes"],
        "bytes_after": after["bytes"],
        "warnings": list(_bounded_diagnostics(warnings)),
    }


def _read_json_dict(path: Path, *, max_bytes: int) -> dict | None:
    try:
        if not path.is_file() or path.stat().st_size > max_bytes:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _json_line(value: dict[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "ACCEPT_CONFIDENCE",
    "DEFAULT_GHOST_ROUTER_ATTEMPTS",
    "DEFAULT_GHOST_ROUTER_NEW_CHAT_TIMEOUT",
    "DEFAULT_GHOST_ROUTER_TIMEOUT",
    "GhostRouteDecision",
    "GhostRouteRequest",
    "GhostRouteResult",
    "GhostRouteStore",
    "GhostRouter",
    "ROUTER_MODES",
    "finalize_route_decision",
    "normalize_route_mode",
    "parse_route_reply",
    "render_route_prompt",
    "route_error_cost",
]