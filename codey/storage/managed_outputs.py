"""Run-scoped storage for large local tool outputs.

Managed outputs are local audit artifacts. They are not prompt context and they
are not a model-readable tool surface.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from codey.policies.action import (
    ActionSubject,
    DECISION_DENY,
    MAX_MANAGED_OUTPUT_BYTES,
    evaluate_action,
)
from codey.storage.local_store import session_key, write_json_atomic
from codey.toolchain import runtime as tool_runtime
from codey.toolchain.runtime import ToolOutcome


MAX_METADATA_BYTES = 16 * 1024
MAX_COMMAND_CHARS = 500
MAX_CWD_CHARS = 240
HANDLE_PREFIX = "out_"
OMITTED_MARKER = "\n[... omitted ...]\n"


@dataclass(frozen=True)
class ManagedOutputRef:
    handle: str
    path: Path
    original_bytes: int
    stored_bytes: int
    sha256: str
    stored_truncated: bool = False


class ManagedOutputStore:
    def __init__(self, state_home: str | Path) -> None:
        if not state_home:
            raise ValueError("state_home required")
        self.root = Path(state_home) / "managed_outputs"

    def write_run_output(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_id: str,
        permission_profile: str,
        command: str,
        cwd: str,
        text: str,
    ) -> ManagedOutputRef | None:
        try:
            run_dir = self._run_dir(session_id, run_id)
            existing = tuple(run_dir.glob(f"{HANDLE_PREFIX}*.json")) if run_dir.exists() else ()
            original_text = str(text or "")
            original_bytes = len(original_text.encode("utf-8"))
            policy = evaluate_action(ActionSubject(
                kind="managed_output",
                phase="managed_output",
                permission_profile=permission_profile,
                byte_count=original_bytes,
                item_count=len(existing),
            ))
            if policy.decision == DECISION_DENY:
                return None
            stored_text, original_bytes, stored_bytes, stored_truncated = _cap_utf8_text(
                original_text,
                MAX_MANAGED_OUTPUT_BYTES,
            )
            digest = hashlib.sha256(stored_text.encode("utf-8")).hexdigest()
            handle = f"{HANDLE_PREFIX}{len(existing) + 1:04d}_{digest[:12]}"
            path = self.path_for(session_id, run_id, handle)
            metadata_path = self.metadata_path_for(session_id, run_id, handle)
            _write_text_atomic(path, stored_text)
            write_json_atomic(
                metadata_path,
                {
                    "schema_version": 1,
                    "handle": handle,
                    "created_at": _now(),
                    "tool_id": _clip(tool_id, 80),
                    "command": _clip(command, MAX_COMMAND_CHARS),
                    "cwd": _clip(cwd or ".", MAX_CWD_CHARS),
                    "original_bytes": original_bytes,
                    "stored_bytes": stored_bytes,
                    "sha256": digest,
                    "stored_truncated": stored_truncated,
                },
                max_bytes=MAX_METADATA_BYTES,
            )
            return ManagedOutputRef(
                handle=handle,
                path=path,
                original_bytes=original_bytes,
                stored_bytes=stored_bytes,
                sha256=digest,
                stored_truncated=stored_truncated,
            )
        except (OSError, TypeError, ValueError):
            return None

    def path_for(self, session_id: str, run_id: str, handle: str) -> Path:
        return self._handle_path(session_id, run_id, handle, ".txt")

    def metadata_path_for(self, session_id: str, run_id: str, handle: str) -> Path:
        return self._handle_path(session_id, run_id, handle, ".json")

    def _run_dir(self, session_id: str, run_id: str) -> Path:
        root = self.root.resolve()
        path = root / session_key(session_id) / _safe_component(run_id, "run")
        resolved = path.resolve()
        resolved.relative_to(root)
        return resolved

    def _handle_path(
        self,
        session_id: str,
        run_id: str,
        handle: str,
        suffix: str,
    ) -> Path:
        safe_handle = _safe_handle(handle)
        path = self._run_dir(session_id, run_id) / f"{safe_handle}{suffix}"
        resolved = path.resolve()
        resolved.relative_to(self.root.resolve())
        return resolved


def run_command_with_managed_output(
    root: Path,
    rel: str,
    command: str,
    *,
    permission_profile: str,
    store: ManagedOutputStore | None,
    session_id: str,
    run_id: str,
    phase: str = "tool_runtime",
    tool_id: str = "",
) -> ToolOutcome:
    raw = tool_runtime.run_command_raw(
        root,
        rel,
        command,
        permission_profile=permission_profile,
        phase=phase,
    )
    if isinstance(raw, ToolOutcome):
        return raw
    projected = tool_runtime.project_run_command_result(root, raw)
    if store is None or not projected.truncated:
        return projected
    ref = store.write_run_output(
        session_id=session_id,
        run_id=run_id,
        tool_id=tool_id,
        permission_profile=permission_profile,
        command=raw.command,
        cwd=rel or ".",
        text=raw.output,
    )
    if ref is None:
        return projected
    managed_output = {
        "handle": ref.handle,
        "original_bytes": ref.original_bytes,
        "stored_bytes": ref.stored_bytes,
        "sha256": ref.sha256,
        "stored_truncated": ref.stored_truncated,
    }
    return ToolOutcome(
        projected.model_text,
        projected.ok,
        canonical=projected.canonical,
        presentation=projected.presentation,
        audit={**dict(projected.audit), "managed_output": managed_output},
        error_code=projected.error_code,
        exit_code=projected.exit_code,
        changed=projected.changed,
        truncated=projected.truncated,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clip(value: object, limit: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text if len(text) <= limit else text[:limit].rstrip()


def _safe_component(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:120].strip("._")
    return text or fallback


def _safe_handle(value: object) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"out_[A-Za-z0-9_.-]{1,80}", text):
        raise ValueError("invalid managed output handle")
    return text


def _cap_utf8_text(text: object, max_bytes: int) -> tuple[str, int, int, bool]:
    value = str(text or "")
    original = value.encode("utf-8")
    if len(original) <= max_bytes:
        return value, len(original), len(original), False
    marker = OMITTED_MARKER.encode("utf-8")
    if max_bytes <= len(marker) + 2:
        clipped = original[:max_bytes].decode("utf-8", errors="ignore")
        stored = clipped.encode("utf-8")
        return clipped, len(original), len(stored), True
    remaining = max_bytes - len(marker)
    head_bytes = max(1, remaining // 2)
    tail_bytes = max(1, remaining - head_bytes)
    clipped = (
        original[:head_bytes].decode("utf-8", errors="ignore")
        + OMITTED_MARKER
        + original[-tail_bytes:].decode("utf-8", errors="ignore")
    )
    stored = clipped.encode("utf-8")
    return clipped, len(original), len(stored), True


def _write_text_atomic(path: Path, text: str) -> None:
    data = text.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass