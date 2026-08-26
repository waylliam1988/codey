"""Durable bounded facts for one local Codey run.

The run ledger is an append-only fact stream, not a transcript.  It deliberately
stores compact event metadata instead of full model replies, source files,
shell output, browser DOM, or webpage text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from codey.runtime.events import RunEvent
from codey.storage.local_store import DEFAULT_STATE_HOME, session_key
from codey.providers.diagnostics import ProviderFailure


SCHEMA_VERSION = 1
MAX_TEXT_CHARS = 1_000
MAX_RESULT_CHARS = 200
MAX_PATH_CHARS = 240
MAX_COMMAND_CHARS = 500
MAX_FAILURE_MESSAGE_CHARS = 500
MAX_CHANGE_FILES = 64
MAX_LEDGER_EVENTS = 512
LEDGER_BYTES_PER_EVENT_BUDGET = 1024
MAX_LEDGER_BYTES = MAX_LEDGER_EVENTS * LEDGER_BYTES_PER_EVENT_BUDGET
TRUNCATED_TEXT_SUFFIX = "..."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clip(value: object, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if limit <= len(TRUNCATED_TEXT_SUFFIX):
        return text[:limit]
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATED_TEXT_SUFFIX)].rstrip() + TRUNCATED_TEXT_SUFFIX


def _safe_file_stem(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:120].strip("._")
    return text or "run"


def _tool_id(event: RunEvent) -> str:
    index = int(event.metadata.get("tool_index") or 0)
    return f"{event.turn}:{index}"


def _event_common(run_id: str, session_id: str, seq: int, event_type: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "seq": seq,
        "ts": _now(),
        "type": event_type,
        "run_id": run_id,
        "session_id": session_id,
    }


def _last_valid_seq(path: Path) -> int:
    last_seq = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
                continue
            seq = _int_or_none(payload.get("seq"))
            if seq is not None and seq > 0:
                last_seq = seq
    except (OSError, UnicodeDecodeError):
        return 0
    return last_seq


@dataclass(frozen=True)
class RunLedgerRecord:
    payload: dict[str, object]


class RunLedgerStore:
    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.state_home = Path(state_home)

    def path_for(self, session_id: str, run_id: str) -> Path:
        return (
            self.state_home
            / "run_ledgers"
            / session_key(session_id)
            / f"{_safe_file_stem(run_id)}.jsonl"
        )

    def open(
        self,
        *,
        run_id: str,
        session_id: str,
        project: str | Path | None,
        task: str,
        provider: str,
        mode: str,
    ) -> "RunLedgerWriter":
        writer = RunLedgerWriter(
            self.path_for(session_id, run_id),
            run_id=run_id,
            session_id=session_id,
        )
        writer.append(
            "run_started",
            project=_clip(project or "", MAX_PATH_CHARS),
            task_chars=len(str(task or "")),
            task_excerpt=_clip(task, MAX_TEXT_CHARS),
            provider=_clip(provider, 80),
            mode=_clip(mode, 40),
        )
        writer.append("provider_selected", provider=_clip(provider, 80))
        return writer


class RunLedgerWriter:
    def __init__(self, path: Path, *, run_id: str, session_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.session_id = session_id
        self.seq = _last_valid_seq(path)
        # Reopening an existing ledger file (resume path) must count its
        # current size, or the byte budget silently restarts from zero and
        # the cap is exceeded.
        try:
            self.bytes_written = path.stat().st_size if path.is_file() else 0
        except OSError:
            self.bytes_written = 0
        self.disabled = False
        self.truncated = False

    def append(self, event_type: str, **fields: object) -> None:
        if self.disabled:
            return
        payload = {
            **_event_common(self.run_id, self.session_id, self.seq + 1, event_type),
            **{key: value for key, value in fields.items() if value is not None},
        }
        self._append_payload(payload)

    def append_run_event(self, event: RunEvent) -> None:
        payload = self._payload_from_run_event(event)
        if payload is not None:
            self._append_payload(payload)
        if event.kind != "tool" or event.call is None or event.outcome is None:
            return
        path = str(event.call.args.get("path") or "")
        if event.call.name == "edit" and event.outcome.ok and event.outcome.changed:
            self.append(
                "file_changed",
                turn=event.turn,
                tool_id=_tool_id(event),
                path="" if path == "." else _clip(path, MAX_PATH_CHARS),
            )
        if event.call.name == "run" and event.outcome.ok and event.outcome.exit_code == 0:
            self.append(
                "command_verified",
                turn=event.turn,
                tool_id=_tool_id(event),
                command=_clip(event.call.args.get("command"), MAX_COMMAND_CHARS),
                cwd="." if path == "." else _clip(path, MAX_PATH_CHARS),
            )

    def append_provider_failure(self, provider: str, failure: ProviderFailure) -> None:
        self.append(
            "provider_failure",
            provider=_clip(provider, 80),
            action=_clip(getattr(failure, "action", ""), 80) or None,
            kind=_clip(getattr(failure, "kind", ""), 120) or None,
            stage=_clip(getattr(failure, "stage", ""), 120) or None,
            message=_clip(getattr(failure, "message", ""), MAX_FAILURE_MESSAGE_CHARS) or None,
        )

    def append_changes_collected(
        self,
        changes: dict | None,
        *,
        checks_passed: bool | None = None,
        receipt: dict | None = None,
    ) -> None:
        if not isinstance(changes, dict):
            changes = {}
        source_files = changes.get("files") if isinstance(changes.get("files"), list) else []
        files = []
        for item in source_files:
            if not isinstance(item, dict):
                continue
            path = _clip(item.get("path"), MAX_PATH_CHARS)
            if not path:
                continue
            files.append({
                "path": path,
                "status": _clip(item.get("status"), 40),
                "additions": _int_or_none(item.get("additions")),
                "deletions": _int_or_none(item.get("deletions")),
            })
            if len(files) >= MAX_CHANGE_FILES:
                break
        self.append(
            "changes_collected",
            ok=bool(changes.get("ok", True)),
            mode=_clip(changes.get("mode"), 40),
            changed_count=_int_or_none(changes.get("changed_count")) or 0,
            files=files,
            files_truncated=len(source_files) > MAX_CHANGE_FILES,
            checks_passed=bool(checks_passed) if checks_passed is not None else None,
            receipt=receipt if isinstance(receipt, dict) else None,
        )

    def finish(self, **fields: object) -> None:
        bounded = {
            "summary_chars": len(str(fields.get("summary") or "")),
            "stop_reason": _clip(fields.get("stop_reason"), 80),
            "turns": _int_or_none(fields.get("turns")) or 0,
            "max_turns": _int_or_none(fields.get("max_turns")) or 0,
            "provider": _clip(fields.get("provider"), 80),
        }
        self.append("run_finished", **bounded)

    def _payload_from_run_event(self, event: RunEvent) -> dict[str, object] | None:
        if event.kind == "turn":
            payload = _event_common(self.run_id, self.session_id, self.seq + 1, "model_reply")
            payload["turn"] = event.turn
            payload["reply_chars"] = len(event.reply or "")
            if event.note:
                payload["note"] = _clip(event.note, MAX_TEXT_CHARS)
            return payload
        if event.kind in {"info", "status"}:
            payload = _event_common(self.run_id, self.session_id, self.seq + 1, event.kind)
            payload["text"] = _clip(event.message, MAX_TEXT_CHARS)
            return payload
        if event.kind == "tool_start" and event.call is not None:
            path = str(event.call.args.get("path") or "")
            payload = _event_common(self.run_id, self.session_id, self.seq + 1, "tool_started")
            payload.update({
                "turn": event.turn,
                "tool_id": _tool_id(event),
                "tool": _clip(event.call.name, 80),
                "path": "" if path == "." else _clip(path, MAX_PATH_CHARS),
                "activity": _clip(event.message, MAX_TEXT_CHARS),
            })
            command = _clip(event.call.args.get("command"), MAX_COMMAND_CHARS)
            if command:
                payload["command"] = command
            return payload
        if event.kind != "tool" or event.call is None or event.outcome is None:
            return None
        path = str(event.call.args.get("path") or "")
        payload = _event_common(self.run_id, self.session_id, self.seq + 1, "tool_finished")
        payload.update({
            "turn": event.turn,
            "tool_id": _tool_id(event),
            "tool": _clip(event.call.name, 80),
            "path": "" if path == "." else _clip(path, MAX_PATH_CHARS),
            "ok": event.outcome.ok,
            "status": _clip(event.outcome.presentation_status(), 40),
            "changed": event.outcome.changed,
            "truncated": event.outcome.truncated,
            "result": _clip(
                event.outcome.presentation_result(MAX_RESULT_CHARS),
                MAX_RESULT_CHARS,
            ),
        })
        command = _clip(event.call.args.get("command"), MAX_COMMAND_CHARS)
        if command:
            payload["command"] = command
        if event.outcome.exit_code is not None:
            payload["exit_code"] = event.outcome.exit_code
        managed = event.outcome.managed_output()
        if managed:
            payload["output_handle"] = _clip(managed.get("handle"), 120)
            payload["output_bytes"] = _int_or_none(managed.get("original_bytes")) or 0
            payload["output_stored_bytes"] = (
                _int_or_none(managed.get("stored_bytes")) or 0
            )
            payload["output_sha256"] = _clip(managed.get("sha256"), 80)
        return payload

    def _append_payload(self, payload: dict[str, object]) -> None:
        try:
            line = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ) + "\n"
            next_size = self.bytes_written + len(line.encode("utf-8"))
            if next_size > MAX_LEDGER_BYTES:
                self._append_truncated_once()
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
            self.seq = int(payload["seq"])
            self.bytes_written = next_size
        except (OSError, TypeError, ValueError):
            self.disabled = True

    def _append_truncated_once(self) -> None:
        if self.truncated or self.disabled:
            return
        self.truncated = True
        payload = {
            **_event_common(self.run_id, self.session_id, self.seq + 1, "ledger_truncated"),
            "max_bytes": MAX_LEDGER_BYTES,
        }
        try:
            line = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
            self.seq = int(payload["seq"])
            self.bytes_written += len(line.encode("utf-8"))
        except OSError:
            pass
        self.disabled = True


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_ledger(path: Path) -> list[RunLedgerRecord]:
    rows: list[RunLedgerRecord] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("schema_version") == SCHEMA_VERSION:
                rows.append(RunLedgerRecord(payload))
    except (OSError, UnicodeDecodeError):
        return []
    return rows
