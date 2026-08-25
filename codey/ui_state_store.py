"""Durable UI session state for the native Codey shell."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codey.local_store import DEFAULT_STATE_HOME, read_json, write_json_atomic


SCHEMA_VERSION = 1
MAX_UI_STATE_BYTES = 16 * 1024 * 1024
MAX_SESSIONS = 200
MAX_PROJECTS = 100
MAX_MESSAGES_PER_SESSION = 500
MAX_FILES_PER_MESSAGE = 20
MAX_STRING = 200_000
MAX_VISIBLE_EXCERPT_CHARS = 12_000
MAX_VISIBLE_EXCERPT_MESSAGES = 20
MAX_EXCERPT_TEXT_CHARS = 1_500
MAX_SHORT_STRING = 1_000
MAX_TITLE = 160
MESSAGE_KEYS = {
    "type",
    "text",
    "note",
    "n",
    "kind",
    "path",
    "result",
    "error",
    "sessionId",
    "id",
    "command",
    "cwd",
    "output",
    "exitCode",
    "approved",
    "count",
    "files",
    "project",
    "eventKey",
    "runId",
    "changed",
    "riskLabel",
    "riskTitle",
    "riskDetail",
    # Pending tool-approval and activity fields the UI round-trips; dropping
    # them silently broke restore of in-flight cards and spinners.
    "toolKey",
    "activity",
    "pending",
}


def _empty_state() -> dict[str, Any]:
    return {
        "active_id": "",
        "sessions": [],
        "projects": [],
        "updated_at": 0,
        "revision": 0,
    }


def _int(value: object) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _str(value: object, limit: int = MAX_SHORT_STRING) -> str:
    return str(value or "")[:limit]


def _clean_files(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    files: list[dict[str, Any]] = []
    for item in value[:MAX_FILES_PER_MESSAGE]:
        if not isinstance(item, dict):
            continue
        files.append(
            {
                "path": _str(item.get("path")),
                "status": _str(item.get("status"), 20),
                "additions": _int(item.get("additions")),
                "deletions": _int(item.get("deletions")),
            }
        )
    return files


def _clean_message(message: object) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    clean: dict[str, Any] = {}
    for key in MESSAGE_KEYS:
        if key not in message:
            continue
        value = message[key]
        if key == "files":
            clean[key] = _clean_files(value)
        elif isinstance(value, bool):
            clean[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            clean[key] = _int(value)
        elif value is None:
            clean[key] = None
        elif key in {"text", "output", "result", "command"}:
            clean[key] = _str(value, MAX_STRING)
        else:
            clean[key] = _str(value)
    return clean if clean else None


def _clean_messages(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    messages: list[dict[str, Any]] = []
    for message in value[-MAX_MESSAGES_PER_SESSION:]:
        clean = _clean_message(message)
        if clean is not None:
            messages.append(clean)
    return messages


MAX_RESEARCH_RUNS_PER_SESSION = 32
MAX_RESEARCH_LIST_ITEMS = 64
MAX_RESEARCH_OBJECT_KEYS = 24
MAX_RESEARCH_DEPTH = 2
RESEARCH_RUN_TEXT_FIELDS = ("runId", "synthesisId", "receipt")
RESEARCH_RUN_LIST_TEXT_FIELDS = (
    "notesCreated",
    "notesUpdated",
    "sourceUrls",
    "queries",
    "counterpoints",
    "qualityWarnings",
)
RESEARCH_RUN_LIST_OBJECT_FIELDS = (
    "searchResults",
    "openedSources",
    "citationMap",
    "evidenceItems",
)


def _clean_research_json(value: object, *, depth: int = MAX_RESEARCH_DEPTH) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _int(value)
    if isinstance(value, str):
        return _str(value, MAX_STRING)
    if depth <= 0:
        return _str(value, MAX_SHORT_STRING)
    if isinstance(value, list):
        return [
            _clean_research_json(item, depth=depth - 1)
            for item in value[:MAX_RESEARCH_LIST_ITEMS]
        ]
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in list(value.items())[:MAX_RESEARCH_OBJECT_KEYS]:
            token = _str(key, MAX_SHORT_STRING)
            if token:
                clean[token] = _clean_research_json(item, depth=depth - 1)
        return clean
    return _str(value, MAX_SHORT_STRING)


def _clean_research_run(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    run: dict[str, Any] = {}
    for key in RESEARCH_RUN_TEXT_FIELDS:
        run[key] = _str(value.get(key), MAX_STRING if key == "receipt" else MAX_SHORT_STRING)
    for key in RESEARCH_RUN_LIST_TEXT_FIELDS:
        raw = value.get(key)
        run[key] = [
            _str(item, MAX_STRING if key == "sourceUrls" else MAX_SHORT_STRING)
            for item in (raw if isinstance(raw, list) else [])[:MAX_RESEARCH_LIST_ITEMS]
        ]
    for key in RESEARCH_RUN_LIST_OBJECT_FIELDS:
        raw = value.get(key)
        run[key] = [
            _clean_research_json(item, depth=MAX_RESEARCH_DEPTH)
            for item in (raw if isinstance(raw, list) else [])[:MAX_RESEARCH_LIST_ITEMS]
        ]
    run["sourcesRead"] = _int(value.get("sourcesRead"))
    run["coverage"] = (
        _clean_research_json(value.get("coverage"), depth=MAX_RESEARCH_DEPTH)
        if isinstance(value.get("coverage"), dict)
        else {}
    )
    run["restoreable"] = False
    run["createdAt"] = _int(value.get("createdAt"))
    return run


def _clean_research_runs(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value[-MAX_RESEARCH_RUNS_PER_SESSION:]:
        clean = _clean_research_run(item)
        if clean is not None:
            rows.append(clean)
    return rows


def _clean_research(value: object) -> bool:
    return bool(value)


def _clean_sessions(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    sessions: list[dict[str, Any]] = []
    for item in value[:MAX_SESSIONS]:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {
            "id": _str(item.get("id")),
            "title": _str(item.get("title") or "New chat", MAX_TITLE),
            "messages": _clean_messages(item.get("messages")),
            "terminalRuns": [
                _str(run_id) for run_id in (
                    item.get("terminalRuns")
                    if isinstance(item.get("terminalRuns"), list)
                    else []
                )[-32:]
            ],
            "createdAt": _int(item.get("createdAt")),
            "projectId": _str(item.get("projectId")) or None,
            "provider": _str(item.get("provider"), 40),
        }
        # Research fields are preserved when present; the sanitizer never
        # invents empty keys so older payloads stay byte-shape stable.
        if isinstance(item.get("researchRuns"), list):
            entry["researchRuns"] = _clean_research_runs(item.get("researchRuns"))
        if "research" in item:
            entry["research"] = _clean_research(item.get("research"))
        sessions.append(entry)
    return sessions


def _clean_projects(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    projects: list[dict[str, Any]] = []
    for item in value[:MAX_PROJECTS]:
        if not isinstance(item, dict):
            continue
        path = _str(item.get("path"))
        if not path:
            continue
        projects.append(
            {
                "id": _str(item.get("id")),
                "name": _str(item.get("name"), MAX_TITLE),
                "path": path,
                "expanded": item.get("expanded") is not False,
                "createdAt": _int(item.get("createdAt")),
            }
        )
    return projects


def _version(state: dict[str, Any]) -> tuple[int, int]:
    return _int(state.get("updated_at")), _int(state.get("revision"))


def _clean_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _empty_state()
    return {
        "active_id": _str(payload.get("active_id")),
        "sessions": _clean_sessions(payload.get("sessions")),
        "projects": _clean_projects(payload.get("projects")),
        "updated_at": _int(payload.get("updated_at")),
        "revision": _int(payload.get("revision")),
    }


def _message_excerpt(message: dict[str, Any]) -> str:
    kind = str(message.get("type") or "")
    if kind == "user":
        label = "User"
        text = str(message.get("text") or "")
    elif kind == "asst":
        label = "Assistant"
        text = str(message.get("text") or "")
    elif kind == "review":
        label = "Review"
        text = str(message.get("text") or "")
    elif kind == "done":
        label = "Done"
        text = str(message.get("text") or "")
    elif kind == "changes":
        count = _int(message.get("count"))
        files = message.get("files")
        paths = []
        if isinstance(files, list):
            paths = [
                str(item.get("path") or "")
                for item in files
                if isinstance(item, dict) and item.get("path")
            ][:3]
        suffix = f": {', '.join(paths)}" if paths else ""
        return f"Changes: {count} file{'s' if count != 1 else ''}{suffix}"
    else:
        return ""
    text = _str(text, MAX_EXCERPT_TEXT_CHARS).strip()
    return f"{label}: {text}" if text else ""


class UiStateStore:
    """Persist the current visible UI state as one bounded local snapshot.

    ``save`` compares against the last state this process wrote (seeded by
    the first :meth:`load`) instead of re-reading and re-parsing the file on
    every save, so a save costs one write, not a read-parse-write cycle.
    """

    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.path = Path(state_home) / "ui-state.json"
        self._cached_state: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        payload = read_json(self.path, max_bytes=MAX_UI_STATE_BYTES)
        if not payload or payload.get("schema_version") != SCHEMA_VERSION:
            state = _empty_state()
        else:
            state = _clean_payload(payload.get("state"))
        self._cached_state = state
        return state

    def _current(self) -> dict[str, Any]:
        if self._cached_state is not None:
            return self._cached_state
        return self.load()

    def save(self, state: object) -> None:
        clean = _clean_payload(state)
        current = self._current()
        if _version(current) > _version(clean):
            return
        if _version(current) == _version(clean) and current != clean:
            return
        if current == clean:
            return
        write_json_atomic(
            self.path,
            {
                "schema_version": SCHEMA_VERSION,
                "state": clean,
            },
            max_bytes=MAX_UI_STATE_BYTES,
        )
        # Only a successful write becomes the new comparison baseline; a
        # failed write leaves the cache pointing at the last durable state.
        self._cached_state = clean

    def visible_session_excerpt(
        self,
        session_id: str,
        *,
        current_request: str = "",
        limit: int = MAX_VISIBLE_EXCERPT_CHARS,
    ) -> str:
        target = str(session_id or "")
        if not target:
            return ""
        current = str(current_request or "").strip()
        for session in self.load().get("sessions", []):
            if not isinstance(session, dict) or str(session.get("id") or "") != target:
                continue
            messages = session.get("messages")
            if not isinstance(messages, list):
                return ""
            lines: list[str] = []
            title = str(session.get("title") or "").strip()
            if title and title != "New chat":
                lines.append(_str(f"Title: {title}", MAX_EXCERPT_TEXT_CHARS))
            visible_lines: list[str] = []
            for message in reversed(messages):
                if not isinstance(message, dict):
                    continue
                if (
                    current
                    and message.get("type") == "user"
                    and str(message.get("text") or "").strip() == current
                ):
                    continue
                line = _message_excerpt(message)
                if line:
                    visible_lines.append(line)
                    if len(visible_lines) >= MAX_VISIBLE_EXCERPT_MESSAGES:
                        break
            lines.extend(reversed(visible_lines))
            excerpt = "\n".join(lines).strip()
            return _str(excerpt, max(0, limit))
        return ""
