"""Run-scoped storage for large local tool outputs.

Managed outputs are local audit artifacts. They are not prompt context and they
are not a model-readable tool surface.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codey.durable.model import ArtifactRecord

from codey.policies.action import (
    DECISION_DENY,
    MAX_MANAGED_OUTPUT_BYTES,
    ActionSubject,
    evaluate_action,
)
from codey.runtime.core import cancellation
from codey.runtime.core.output_capture import CAPTURE_LIMIT_BYTES
from codey.storage.atomic_io import write_bytes_atomic
from codey.storage.file_lock import with_file_lock
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
    original_sha256: str = ""


@dataclass(frozen=True)
class ManagedJsonArtifactRef:
    """Durable, scoped reference for structured recovery artifacts."""

    session_id: str
    run_id: str
    handle: str
    sha256: str
    schema_version: int

    def to_payload(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "handle": self.handle,
            "sha256": self.sha256,
            "schema_version": self.schema_version,
        }


class ManagedOutputStore:
    def __init__(self, state_home: str | Path) -> None:
        if not state_home:
            raise ValueError("state_home required")
        self.root = Path(state_home) / "managed_outputs"

    def write_tool_output(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_id: str,
        permission_profile: str,
        tool_name: str,
        display_ref: str,
        text: str,
        command: str = "",
        cwd: str = ".",
    ) -> ManagedOutputRef | None:
        try:
            run_dir = self._run_dir(session_id, run_id)
            with with_file_lock(run_dir):
                existing = tuple(run_dir.glob(f"{HANDLE_PREFIX}*.json")) if run_dir.exists() else ()
                original_text = (text or "")
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
                original_digest = hashlib.sha256(original_text.encode("utf-8")).hexdigest()
                handle = f"{HANDLE_PREFIX}{len(existing) + 1:04d}_{digest[:12]}"
                path = self.path_for(session_id, run_id, handle)
                metadata_path = self.metadata_path_for(session_id, run_id, handle)
                write_bytes_atomic(path, stored_text.encode("utf-8"))
                write_json_atomic(
                    metadata_path,
                    {
                        "schema_version": 1,
                        "handle": handle,
                        "created_at": _now(),
                        "tool_id": _clip(tool_id, 80),
                        "tool_name": _clip(tool_name, 80),
                        "display_ref": _clip(display_ref, MAX_CWD_CHARS),
                        "command": _clip(command or f"{tool_name} {display_ref}".strip(), MAX_COMMAND_CHARS),
                        "cwd": _clip(cwd or ".", MAX_CWD_CHARS),
                        "original_bytes": original_bytes,
                        "stored_bytes": stored_bytes,
                        "sha256": digest,
                        "original_sha256": original_digest,
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
                    original_sha256=original_digest,
                )
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            raise
        except (OSError, TypeError, ValueError):
            return None

    def path_for(self, session_id: str, run_id: str, handle: str) -> Path:
        return self._handle_path(session_id, run_id, handle, ".txt")

    def metadata_path_for(self, session_id: str, run_id: str, handle: str) -> Path:
        return self._handle_path(session_id, run_id, handle, ".json")

    def read_tool_output(self, session_id: str, run_id: str, handle: str) -> tuple[str, dict[str, object]]:
        """Read and verify a managed receipt; fail-closed on any problem.

        Returns (text, metadata). Raises ValueError/OSError when the handle
        is malformed, the receipt/metadata is missing, or the stored bytes
        do not match the recorded sha256. Recovery callers map any exception
        to an explicit recovery failure (never silent truncation).
        """
        import json as _json

        safe_handle = _safe_handle(handle)
        text_path = self._handle_path(session_id, run_id, safe_handle, ".txt")
        metadata_path = self._handle_path(session_id, run_id, safe_handle, ".json")
        try:
            with metadata_path.open("rb") as stream:
                metadata_bytes = stream.read(MAX_METADATA_BYTES + 1)
            if len(metadata_bytes) > MAX_METADATA_BYTES:
                raise ValueError("managed receipt metadata exceeds size limit")
            metadata_raw = metadata_bytes.decode("utf-8")
        except OSError as exc:
            raise OSError(f"managed receipt metadata missing for {safe_handle}: {exc}") from exc
        try:
            metadata = _json.loads(metadata_raw)
        except ValueError as exc:
            raise ValueError(f"managed receipt metadata unreadable for {safe_handle}: {exc}") from exc
        if not isinstance(metadata, dict) or metadata.get("handle") != safe_handle:
            raise ValueError(f"managed receipt metadata mismatch for {safe_handle}")
        expected_sha = str(metadata.get("sha256") or "")
        if not _is_sha256_hex(expected_sha):
            raise ValueError(f"managed receipt metadata sha invalid for {safe_handle}")
        try:
            with text_path.open("rb") as stream:
                stored_bytes = stream.read(MAX_MANAGED_OUTPUT_BYTES + 1)
            if len(stored_bytes) > MAX_MANAGED_OUTPUT_BYTES:
                raise ValueError("managed receipt exceeds size limit")
            stored_text = stored_bytes.decode("utf-8")
        except OSError as exc:
            raise OSError(f"managed receipt missing for {safe_handle}: {exc}") from exc
        actual_sha = hashlib.sha256(stored_text.encode("utf-8")).hexdigest()
        if actual_sha != expected_sha:
            raise ValueError(f"managed receipt content mismatch for {safe_handle}")
        return stored_text, {str(k): v for k, v in metadata.items()}

    def durable_record(
        self,
        ref: ManagedOutputRef | str,
        *,
        session_id: str,
        run_id: str,
        owner_task_id: str | None = None,
        owner_effect_id: str | None = None,
    ) -> ArtifactRecord:
        """Build verified durable metadata for a previously written output."""
        from codey.durable.model import ArtifactRecord

        handle = ref.handle if isinstance(ref, ManagedOutputRef) else str(ref)
        _text, metadata = self.read_tool_output(session_id, run_id, handle)
        sha256 = str(metadata.get("sha256") or "")
        stored_bytes = metadata.get("stored_bytes")
        if type(stored_bytes) is not int or stored_bytes < 0:
            raise ValueError("managed output metadata has invalid stored byte count")
        return ArtifactRecord(
            handle=handle,
            sha256=sha256,
            byte_count=stored_bytes,
            storage_path=str(self.path_for(session_id, run_id, handle)),
            owner_task_id=owner_task_id,
            owner_effect_id=owner_effect_id,
            created_at=str(metadata.get("created_at") or _now()),
            metadata=metadata,
        )

    def write_json_artifact(
        self,
        *,
        session_id: str,
        run_id: str,
        payload: object,
        schema_version: int = 1,
    ) -> ManagedJsonArtifactRef:
        """Atomically persist a bounded JSON artifact and its digest metadata."""
        if type(schema_version) is not int or schema_version < 1:
            raise ValueError("schema_version must be a positive integer")
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(raw) > MAX_MANAGED_OUTPUT_BYTES:
            raise ValueError("json artifact exceeds size limit")
        digest = hashlib.sha256(raw).hexdigest()
        handle = f"artifact_{digest[:24]}"
        run_dir = self._run_dir(session_id, run_id)
        with with_file_lock(run_dir):
            path = self._handle_path(session_id, run_id, handle, ".json")
            metadata_path = self._handle_path(session_id, run_id, handle, ".meta.json")
            write_bytes_atomic(path, raw)
            write_json_atomic(
                metadata_path,
                {
                    "schema_version": schema_version,
                    "session_id": session_id,
                    "run_id": run_id,
                    "handle": handle,
                    "sha256": digest,
                    "bytes": len(raw),
                },
                max_bytes=MAX_METADATA_BYTES,
            )
        return ManagedJsonArtifactRef(session_id, run_id, handle, digest, schema_version)

    def read_json_artifact(
        self,
        ref: ManagedJsonArtifactRef | dict[str, object] | str,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
    ) -> object:
        """Read a structured artifact only after scope, schema and digest checks."""
        expected_schema: object | None
        if isinstance(ref, ManagedJsonArtifactRef):
            ref_session, ref_run, handle, expected_digest = ref.session_id, ref.run_id, ref.handle, ref.sha256
            expected_schema = ref.schema_version
        elif isinstance(ref, dict):
            ref_session = str(ref.get("session_id") or session_id or "")
            ref_run = str(ref.get("run_id") or run_id or "")
            handle = str(ref.get("handle") or "")
            expected_digest = str(ref.get("sha256") or "")
            expected_schema = ref.get("schema_version")
        else:
            ref_session, ref_run, handle, expected_digest, expected_schema = (
                (session_id or ""), (run_id or ""), ref, "", None,
            )
        if not ref_session or not ref_run or not handle:
            raise ValueError("json artifact reference is incomplete")
        safe_handle = _safe_artifact_handle(handle)
        path = self._handle_path(ref_session, ref_run, safe_handle, ".json")
        metadata_path = self._handle_path(ref_session, ref_run, safe_handle, ".meta.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError("json artifact metadata is invalid")
        if metadata.get("session_id") != ref_session or metadata.get("run_id") != ref_run:
            raise ValueError("json artifact scope mismatch")
        if metadata.get("handle") != safe_handle or metadata.get("schema_version") != expected_schema:
            raise ValueError("json artifact metadata mismatch")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != metadata.get("sha256") or (expected_digest and digest != expected_digest):
            raise ValueError("json artifact digest mismatch")
        return json.loads(raw.decode("utf-8"))

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
        safe_handle = _safe_artifact_handle(handle) if handle.startswith("artifact_") else _safe_handle(handle)
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
    execution_store: object | None = None,
) -> ToolOutcome:
    raw = tool_runtime.run_command_raw(
        root,
        rel,
        command,
        permission_profile=permission_profile,
        phase=phase,
        # The archive cannot recover bytes discarded by the process pumps.
        # Capture up to its existing capacity; the model preview is clipped later.
        capture_limit_bytes=MAX_MANAGED_OUTPUT_BYTES if store is not None else CAPTURE_LIMIT_BYTES,
        execution_store=execution_store,
        session_id=session_id,
        run_id=run_id,
    )
    if isinstance(raw, ToolOutcome):
        return raw
    projected = tool_runtime.project_run_command_result(root, raw)
    if store is None or not projected.truncated:
        return projected
    ref = store.write_tool_output(
        session_id=session_id,
        run_id=run_id,
        tool_id=tool_id,
        permission_profile=permission_profile,
        tool_name="run",
        display_ref=raw.command,
        text=raw.output,
        command=raw.command,
        cwd=rel or ".",
    )
    if ref is None:
        return ToolOutcome(
            projected.model_text,
            projected.ok,
            canonical=projected.canonical,
            presentation=projected.presentation,
            audit={**dict(projected.audit), "managed_output_failed": True},
            error_code=projected.error_code or "managed_output_failed",
            exit_code=projected.exit_code,
            changed=projected.changed,
            truncated=projected.truncated,
        )
    # Process facts (process_bytes, capture_truncated) already live in the
    # projection audit; this layer only adds the stored artifact receipt,
    # which mirrors ManagedOutputRef and the on-disk JSON verbatim.
    managed_output = {
        "handle": ref.handle,
        "original_bytes": ref.original_bytes,
        "stored_bytes": ref.stored_bytes,
        "sha256": ref.sha256,
        "original_sha256": ref.original_sha256,
        "stored_truncated": ref.stored_truncated,
    }
    audit = {**dict(projected.audit), "managed_output": managed_output}
    return ToolOutcome(
        projected.model_text,
        projected.ok,
        canonical=projected.canonical,
        presentation=projected.presentation,
        audit=audit,
        error_code=projected.error_code,
        exit_code=projected.exit_code,
        changed=projected.changed,
        truncated=projected.truncated,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


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


def _safe_artifact_handle(value: object) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"artifact_[A-Za-z0-9_.-]{1,80}", text):
        raise ValueError("invalid managed artifact handle")
    return text


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


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
