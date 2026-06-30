"""Bounded persistence for seamless conversation continuation."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

from codey.handoff import (
    DEFAULT_HARD_CONTEXT_TOKENS,
    MAX_HANDOFF_FILES,
    MAX_MODEL_SUMMARY_CHARS,
    ConversationContext,
    ConversationSnapshot,
    compact_text,
)
from codey.local_store import (
    DEFAULT_STATE_HOME,
    delete_file,
    read_json,
    session_key,
    write_json_atomic,
)


SCHEMA_VERSION = 1
MAX_PERSISTED_CONVERSATIONS = 64
SNAPSHOT_FIELDS = {field.name for field in fields(ConversationSnapshot)}


def _snapshot_payload(snapshot: ConversationSnapshot) -> dict:
    return {
        "mode": snapshot.mode,
        "goal": compact_text(snapshot.goal),
        "project": snapshot.project,
        "provider_id": snapshot.provider_id,
        "changed_files": list(dict.fromkeys(snapshot.changed_files))[:MAX_HANDOFF_FILES],
        "checks_passed": snapshot.checks_passed,
        "summary": compact_text(snapshot.summary),
        "blocker": compact_text(snapshot.blocker),
        "latest_user": compact_text(snapshot.latest_user),
        "latest_reply": compact_text(snapshot.latest_reply),
        "conversation_summary": compact_text(
            snapshot.conversation_summary,
            MAX_MODEL_SUMMARY_CHARS,
        ),
    }


def _snapshot_from_payload(payload: object) -> ConversationSnapshot:
    if not isinstance(payload, dict):
        return ConversationSnapshot(mode="chat")
    clean = {key: value for key, value in payload.items() if key in SNAPSHOT_FIELDS}
    files = clean.get("changed_files")
    clean["changed_files"] = (
        tuple(str(item) for item in files[:MAX_HANDOFF_FILES])
        if isinstance(files, list)
        else ()
    )
    for key in (
        "mode",
        "goal",
        "project",
        "provider_id",
        "summary",
        "blocker",
        "latest_user",
        "latest_reply",
    ):
        clean[key] = compact_text(str(clean.get(key) or ""))
    clean["conversation_summary"] = compact_text(
        str(clean.get("conversation_summary") or ""),
        MAX_MODEL_SUMMARY_CHARS,
    )
    checks = clean.get("checks_passed")
    clean["checks_passed"] = checks if isinstance(checks, bool) else None
    return ConversationSnapshot(**clean)


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class ConversationStore:
    """Keep one compact factual snapshot for each recent local chat."""

    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.directory = Path(state_home) / "conversations"

    def path_for(self, session_id: str) -> Path:
        return self.directory / f"{session_key(session_id)}.json"

    def load(self, session_id: str) -> ConversationContext:
        payload = read_json(self.path_for(session_id))
        if not payload or payload.get("schema_version") != SCHEMA_VERSION:
            return ConversationContext()
        return ConversationContext(
            hard_limit=DEFAULT_HARD_CONTEXT_TOKENS,
            used_tokens=_nonnegative_int(payload.get("used_tokens")),
            provider_id=str(payload.get("provider_id") or ""),
            mode=str(payload.get("mode") or ""),
            project=str(payload.get("project") or ""),
            initialized=bool(payload.get("initialized")),
            handoff_summary=compact_text(
                str(payload.get("handoff_summary") or ""),
                MAX_MODEL_SUMMARY_CHARS,
            ),
            snapshot=_snapshot_from_payload(payload.get("snapshot")),
        )

    def save(self, session_id: str, context: ConversationContext) -> None:
        path = self.path_for(session_id)
        write_json_atomic(
            path,
            {
                "schema_version": SCHEMA_VERSION,
                "used_tokens": max(0, context.used_tokens),
                "provider_id": context.provider_id,
                "mode": context.mode,
                "project": context.project,
                "initialized": context.initialized,
                "handoff_summary": compact_text(
                    context.handoff_summary,
                    MAX_MODEL_SUMMARY_CHARS,
                ),
                "snapshot": _snapshot_payload(context.snapshot),
            },
        )
        self._prune(path)

    def delete(self, session_id: str) -> None:
        delete_file(self.path_for(session_id))

    def _prune(self, keep: Path) -> None:
        try:
            paths = sorted(
                (path for path in self.directory.glob("*.json") if path != keep),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for path in paths[MAX_PERSISTED_CONVERSATIONS - 1:]:
            delete_file(path)
