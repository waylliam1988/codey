"""Durable bounded facts for one local Codey run.

The run ledger is an append-only fact stream, not a transcript.  It deliberately
stores compact event metadata instead of full model replies, source files,
shell output, browser DOM, or webpage text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from codey.providers.diagnostics import ProviderFailure
from codey.runs.receipt import task_receipt_from_payload
from codey.runtime.observe.events import RunEvent
from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import DEFAULT_STATE_HOME, session_key

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
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


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
    corrupt: bool = False


def _scan_ledger_file(path: Path) -> tuple[list[dict[str, object]], bool, int, bool, int]:
    """Strict scan: any bad row makes the stream not projectable.

    Bad JSON, bad schema, or a non-continuous ``seq`` all mark the file
    incomplete. Callers never project a prefix of a damaged stream.
    """
    try:
        exists = path.is_file()
    except OSError:
        return [], False, 0, False, 0
    if not exists:
        return [], True, 0, False, 0
    try:
        bytes_written = path.stat().st_size
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return [], False, 0, False, 0
    if not lines:
        return [], True, 0, False, bytes_written
    payloads: list[dict[str, object]] = []
    expected_seq = 1
    truncated = False
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return [], False, 0, False, bytes_written
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            return [], False, 0, False, bytes_written
        seq = _int_or_none(payload.get("seq"))
        if seq is None or seq != expected_seq:
            return [], False, 0, False, bytes_written
        expected_seq += 1
        if payload.get("type") == "ledger_truncated":
            truncated = True
        payloads.append(payload)
    return payloads, True, expected_seq - 1, truncated, bytes_written


def _ledger_file_state(path: Path) -> _LedgerFileState:
    _payloads, complete, last_seq, truncated, bytes_written = _scan_ledger_file(path)
    if complete:
        return _LedgerFileState(
            seq=last_seq,
            bytes_written=bytes_written,
            truncated=truncated,
            corrupt=False,
        )
    try:
        size = path.stat().st_size if path.is_file() else 0
    except OSError:
        size = 0
    return _LedgerFileState(seq=0, bytes_written=size, truncated=False, corrupt=True)


class LedgerWriteFailed(OSError):
    """Durable ledger write failed; the run's ledger is unavailable."""


def _stat_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns if path.is_file() else 0
    except OSError:
        return 0


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
    ) -> RunLedgerWriter:
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
        self.disabled = self.truncated or file_state.corrupt
        self._mtime_ns = _stat_mtime_ns(path)
        # Observable failure state (cold start, in-memory only): callers
        # and tests read these instead of guessing from missing rows.
        if file_state.corrupt:
            self.disabled_reason: str = "ledger_corrupt"
            self.last_error_reason: str = "ledger corrupt"
        else:
            self.disabled_reason: str = "ledger_truncated" if self.truncated else ""
            self.last_error_reason: str = ""

    def append(self, event_type: str, **fields: object) -> None:
        if self.disabled:
            # Truncation is an expected capacity signal; IO failure and
            # corruption must surface so hooks can mark this run's ledger
            # unavailable instead of extending a damaged stream.
            if self.disabled_reason in ("ledger_write_failed", "ledger_corrupt"):
                raise LedgerWriteFailed(self.last_error_reason or "ledger unavailable")
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
            if self.disabled_reason in ("ledger_write_failed", "ledger_corrupt"):
                raise LedgerWriteFailed(self.last_error_reason or "ledger unavailable")
            return
        try:
            with with_file_lock(self.path):
                current_seq, current_bytes, truncated = self._fast_file_state_locked()
                if truncated is None:
                    file_state = _ledger_file_state(self.path)
                    if file_state.corrupt:
                        self.seq = 0
                        self.bytes_written = file_state.bytes_written
                        self.truncated = False
                        self.disabled = True
                        self.disabled_reason = "ledger_corrupt"
                        self.last_error_reason = "ledger corrupt"
                        raise LedgerWriteFailed(self.last_error_reason)
                    current_seq = file_state.seq
                    current_bytes = file_state.bytes_written
                    truncated = file_state.truncated
                if truncated and not allow_after_truncation:
                    self.seq = current_seq
                    self.bytes_written = current_bytes
                    self.truncated = True
                    self.disabled = True
                    self.disabled_reason = "ledger_truncated"
                    return
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
                self._mtime_ns = _stat_mtime_ns(self.path)
        except (OSError, TimeoutError, TypeError, ValueError) as exc:
            self.disabled = True
            self.disabled_reason = "ledger_write_failed"
            self.last_error_reason = _clip(f"{type(exc).__name__}: {exc}", 120)
            raise LedgerWriteFailed(self.last_error_reason) from exc

    def _fast_file_state_locked(self) -> tuple[int, int, bool | None]:
        """In-memory seq/bytes fast path; None means fall back to a full scan.

        The writer owns the file for its run, so when on-disk size *and*
        mtime still match the last write the cached seq is authoritative and
        no parse is needed. Any drift -- including a same-size content change,
        which always bumps mtime -- falls back to _ledger_file_state, which
        recomputes seq from content. Deliberate mtime-preserving tampering is
        out of scope: the ledger is a local crash-recovery log, not a
        tamper-evidence store.
        """
        try:
            stat = self.path.stat() if self.path.is_file() else None
        except OSError:
            return self.seq, self.bytes_written, self.truncated or None
        if stat is None:
            if self.bytes_written == 0:
                return self.seq, self.bytes_written, self.truncated
            return self.seq, self.bytes_written, None
        if stat.st_size == self.bytes_written and stat.st_mtime_ns == self._mtime_ns:
            return self.seq, self.bytes_written, self.truncated
        return self.seq, self.bytes_written, None

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
        self._mtime_ns = _stat_mtime_ns(self.path)
        self.disabled = True
        self.disabled_reason = "ledger_truncated"

    def _write_line_locked(self, line: str) -> None:
        from codey.storage.atomic_io import append_bytes_durable

        append_bytes_durable(self.path, [line.encode("utf-8")])


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_ledger(path: Path) -> list[RunLedgerRecord]:
    """Read ledger rows; any damage yields unavailable (empty).

    A damaged stream -- bad JSON, bad schema, non-continuous ``seq``,
    torn tail included -- never projects a readable prefix into receipts
    or Ghost learning. Callers treat ``[]`` as "no complete facts".
    """
    payloads, complete, _seq, _truncated, _size = _scan_ledger_file(path)
    if not complete:
        return []
    return [RunLedgerRecord(payload) for payload in payloads]
