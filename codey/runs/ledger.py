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
from codey.storage.file_lock import with_file_lock
from codey.runs.receipt import task_receipt_from_payload
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


def _json_line(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


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


@dataclass(frozen=True)
class _LedgerFileState:
    seq: int
    bytes_written: int
    truncated: bool


def _ledger_file_state(path: Path) -> _LedgerFileState:
    last_seq = 0
    truncated = False
    try:
        bytes_written = path.stat().st_size if path.is_file() else 0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
                continue
            seq = _int_or_none(payload.get("seq"))
            if seq is not None and seq > 0:
                last_seq = max(last_seq, seq)
            if payload.get("type") == "ledger_truncated":
                truncated = True
    except (OSError, UnicodeDecodeError):
        return _LedgerFileState(seq=0, bytes_written=0, truncated=False)
    return _LedgerFileState(
        seq=last_seq,
        bytes_written=bytes_written,
        truncated=truncated,
    )


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
        file_state = _ledger_file_state(path)
        self.seq = file_state.seq
        self.bytes_written = file_state.bytes_written
        self.truncated = file_state.truncated
        self.disabled = self.truncated

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
        validated_receipt = task_receipt_from_payload(receipt)
        self.append(
            "changes_collected",
            ok=bool(changes.get("ok", True)),
            mode=_clip(changes.get("mode"), 40),
            changed_count=_int_or_none(changes.get("changed_count")) or 0,
            files=files,
            files_truncated=len(source_files) > MAX_CHANGE_FILES,
            checks_passed=bool(checks_passed) if checks_passed is not None else None,
            # The receipt enters the durable stream only in full schema-v1
            # shape: the projection layer rebuilds the user-visible receipt
            # from this row, so a schema-incomplete or malformed payload must never
            # land here.
            receipt=validated_receipt.to_dict() if validated_receipt is not None else None,
        )

    def finish(self, **fields: object) -> None:
        bounded = {
            "summary_chars": len(str(fields.get("summary") or "")),
            "stop_reason": _clip(fields.get("stop_reason"), 80),
            "turns": _int_or_none(fields.get("turns")) or 0,
            "max_turns": _int_or_none(fields.get("max_turns")) or 0,
            "provider": _clip(fields.get("provider"), 80),
        }
        payload = {
            **_event_common(self.run_id, self.session_id, self.seq + 1, "run_finished"),
            **{key: value for key, value in bounded.items() if value is not None},
        }
        self._append_payload(
            payload,
            allow_after_truncation=True,
            allow_over_budget=True,
        )

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

    def _append_payload(
        self,
        payload: dict[str, object],
        *,
        allow_after_truncation: bool = False,
        allow_over_budget: bool = False,
    ) -> None:
        if self.disabled and not (allow_after_truncation and self.truncated):
            return
        try:
            with with_file_lock(self.path):
                file_state = _ledger_file_state(self.path)
                if file_state.truncated and not allow_after_truncation:
                    self.seq = file_state.seq
                    self.bytes_written = file_state.bytes_written
                    self.truncated = True
                    self.disabled = True
                    return
                current_seq = file_state.seq
                current_bytes = file_state.bytes_written
                payload = dict(payload)
                payload["seq"] = current_seq + 1
                line = _json_line(payload)
                next_size = current_bytes + len(line.encode("utf-8"))
                if not allow_over_budget and next_size > MAX_LEDGER_BYTES:
                    self._append_truncated_once_locked(current_seq, current_bytes)
                    return
                self._write_line_locked(line)
                self.seq = int(payload["seq"])
                self.bytes_written = next_size
        except (OSError, TimeoutError, TypeError, ValueError):
            self.disabled = True

    def _append_truncated_once_locked(self, current_seq: int, current_bytes: int) -> None:
        if self.truncated:
            return
        self.truncated = True
        payload = {
            **_event_common(self.run_id, self.session_id, current_seq + 1, "ledger_truncated"),
            "max_bytes": MAX_LEDGER_BYTES,
        }
        line = _json_line(payload)
        self._write_line_locked(line)
        self.seq = int(payload["seq"])
        self.bytes_written = current_bytes + len(line.encode("utf-8"))
        self.disabled = True

    def _write_line_locked(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)


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
