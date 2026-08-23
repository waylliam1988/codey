"""Deterministic AnalysisRun projections for audited local command runs.

This module projects already-normalized run-command execution facts into
bounded AnalysisRun records. Inputs are plain mappings produced by the runtime
side; this module never imports runtime layers, never performs I/O, and never
stores raw command output.

v1 scope notes:
- No script_hash / dependency_fingerprint / git fields: no producer exists yet.
- ``reproduction_status`` only reports what v1 can honestly know: whether an
  output artifact was captured and whether the command itself failed.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Mapping

from codey.refs import (
    clip,
    digest_json,
    digest_text,
    stable_ref,
)
from codey.redaction import looks_sensitive_signal
from codey.research.identity import path_ref

ANALYSIS_RUN_REF_PREFIX = "analysis_run:"
CAPTURE_OUTPUT_CAPTURED = "output_captured"
CAPTURE_NOT_CAPTURED = "output_not_captured"
REPRODUCTION_FAILED = "failed"
MAX_COMMAND_DISPLAY_CHARS = 500
MAX_WARNING_CHARS = 120
MAX_WARNINGS = 8
_MAX_DURATION_MS = 10**9

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOOL_INSTANCE_RE = re.compile(r"^\d+:\d+$")


@dataclass(frozen=True)
class AnalysisRunRecord:
    analysis_run_id: str
    run_id: str
    tool_id: str
    tool_name: str
    command_digest: str
    command_display: str
    cwd_ref: dict[str, object]
    exit_code: int | None
    ok: bool
    started_at: str
    finished_at: str
    duration_ms: int | None
    managed_output_handle: str
    output_sha256: str
    stored_truncated: bool
    capture_quality: str
    reproduction_status: str
    environment_digest: str
    warnings: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "analysis_run_id": self.analysis_run_id,
            "run_id": self.run_id,
            "tool_id": self.tool_id,
            "tool_name": self.tool_name,
            "command_digest": self.command_digest,
            "command_display": self.command_display,
            "cwd_ref": dict(self.cwd_ref),
            "exit_code": self.exit_code,
            "ok": self.ok,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "managed_output_handle": self.managed_output_handle,
            "output_sha256": self.output_sha256,
            "stored_truncated": self.stored_truncated,
            "capture_quality": self.capture_quality,
            "reproduction_status": self.reproduction_status,
            "environment_digest": self.environment_digest,
            "warnings": list(self.warnings),
        }


def _clean_sha256(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if _SHA256_RE.fullmatch(text) else ""


def _bounded_duration(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return min(parsed, _MAX_DURATION_MS)


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _managed_output_view(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _tool_instance_id(value: object) -> str:
    text = clip(value, 40)
    return text if _TOOL_INSTANCE_RE.fullmatch(text) else ""


def environment_summary_digest() -> str:
    """Digest of a minimal allow-listed environment summary."""

    summary = {
        "python": f"{sys.version_info[0]}.{sys.version_info[1]}",
        "platform": str(sys.platform),
    }
    return digest_json(summary)


def analysis_run_record(data: Mapping[str, object]) -> AnalysisRunRecord | None:
    """Project one bounded execution-fact mapping into an AnalysisRunRecord.

    Returns None when there is nothing auditable (missing command), so callers
    can fail open without recording partial facts.
    """

    if not isinstance(data, Mapping):
        return None
    command = str(data.get("command") or "").strip()
    if not command:
        return None
    tool_id = _tool_instance_id(data.get("tool_id"))
    tool_name = clip(data.get("tool_name"), 40)
    if not tool_id or not tool_name:
        return None

    warnings: list[str] = []
    started_at = clip(data.get("started_at"), 40)
    finished_at = clip(data.get("finished_at"), 40)
    duration_ms = _bounded_duration(data.get("duration_ms"))
    ok = bool(data.get("ok"))
    if ok and duration_ms is None:
        warnings.append("timing_unavailable")

    managed = _managed_output_view(data.get("managed_output"))
    handle = clip(managed.get("handle"), 80)
    output_sha256 = _clean_sha256(managed.get("sha256"))
    captured = bool(handle and output_sha256)
    if handle and not output_sha256:
        warnings.append("managed_output_sha_invalid")

    # The display command is a convenience, never a provenance fact: the
    # digest above is authoritative. Secret-looking commands keep only their
    # digest, matching ProjectFacts' refusal to persist such commands.
    if looks_sensitive_signal(command):
        warnings.append("command_display_redacted")
        command_display = ""
    else:
        command_display = clip(command, MAX_COMMAND_DISPLAY_CHARS)

    exit_code = _optional_int(data.get("exit_code"))
    record = AnalysisRunRecord(
        analysis_run_id=stable_ref(
            ANALYSIS_RUN_REF_PREFIX.removesuffix(":"),
            digest_text(command),
            tool_id,
            started_at,
            exit_code,
        ),
        run_id=clip(data.get("run_id"), 120),
        tool_id=tool_id,
        tool_name=tool_name,
        command_digest=digest_text(command),
        command_display=command_display,
        cwd_ref=path_ref(str(data.get("cwd") or "."), project=str(data.get("project") or "")),
        exit_code=exit_code,
        ok=ok,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        managed_output_handle=handle,
        output_sha256=output_sha256,
        stored_truncated=bool(managed.get("stored_truncated")),
        capture_quality=(
            CAPTURE_OUTPUT_CAPTURED if captured else CAPTURE_NOT_CAPTURED
        ),
        reproduction_status=(
            REPRODUCTION_FAILED if not ok else (
                CAPTURE_OUTPUT_CAPTURED if captured else CAPTURE_NOT_CAPTURED
            )
        ),
        environment_digest=environment_summary_digest(),
        warnings=tuple(warnings[:MAX_WARNINGS]),
    )
    return record


__all__ = [
    "ANALYSIS_RUN_REF_PREFIX",
    "CAPTURE_NOT_CAPTURED",
    "CAPTURE_OUTPUT_CAPTURED",
    "AnalysisRunRecord",
    "REPRODUCTION_FAILED",
    "analysis_run_record",
    "environment_summary_digest",
]
