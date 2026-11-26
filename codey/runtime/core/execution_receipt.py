"""Execution facts shared by model delivery and approval reconstruction."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal

from codey.runtime.core.models import ToolResult
from codey.types.json_value import JsonObject, require_json_boolean, require_json_integer, require_json_string
from codey.utils.text_budget import clip_middle


@dataclass(frozen=True)
class ExecutionOutputReference:
    execution_id: str
    session_id: str
    run_id: str
    revision: int
    sha256: str

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, object]) -> ExecutionOutputReference:
        raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return cls(require_json_string(snapshot["execution_id"], "execution_id"),
                   require_json_string(snapshot["session_id"], "session_id"),
                   require_json_string(snapshot["run_id"], "run_id"),
                   require_json_integer(snapshot["revision"], "revision"),
                   hashlib.sha256(raw.encode("utf-8")).hexdigest())

    @classmethod
    def from_payload(cls, payload: object) -> ExecutionOutputReference:
        if not isinstance(payload, dict) or set(payload) != {"version", "execution_id", "session_id", "run_id", "revision", "sha256"}:
            raise ValueError("invalid execution output reference")
        if type(payload["version"]) is not int or payload["version"] != 1:
            raise ValueError("invalid execution output reference version")
        ref = cls(require_json_string(payload["execution_id"], "execution_id"),
                  require_json_string(payload["session_id"], "session_id"),
                  require_json_string(payload["run_id"], "run_id"),
                  require_json_integer(payload["revision"], "revision"), require_json_string(payload["sha256"], "sha256"))
        if (not ref.execution_id or not ref.session_id or not ref.run_id or ref.revision < 0
                or len(ref.sha256) != 64 or any(char not in "0123456789abcdef" for char in ref.sha256)):
            raise ValueError("invalid execution output reference identity")
        return ref

    def facts(self) -> JsonObject:
        return {"version": 1, "execution_id": self.execution_id, "session_id": self.session_id,
                "run_id": self.run_id, "revision": self.revision, "sha256": self.sha256}


@dataclass(frozen=True)
class ExecutionReceipt:
    status: str
    exit_code: int | None
    output: str
    truncated: bool = False
    capture_truncated: bool = False
    execution_id: str = ""
    exit_code_scope: Literal["shell", "process"] = "shell"
    full_output_retained: bool = False
    output_ref: ExecutionOutputReference | None = None

    @classmethod
    def from_facts(cls, payload: Mapping[str, object]) -> ExecutionReceipt:
        fields = {"status", "exit_code", "exit_code_scope", "truncated", "capture_truncated", "execution_id",
                  "full_output_retained", "full_output_recoverable", "output_ref"}
        if set(payload) != fields:
            raise ValueError("invalid execution receipt fields")
        result = cls.from_shell_result(payload)
        recoverable = require_json_boolean(payload["full_output_recoverable"], "full_output_recoverable")
        if recoverable != result.full_output_recoverable:
            raise ValueError("execution receipt recoverability differs from its facts")
        if result.full_output_retained and result.output_ref is None:
            raise ValueError("retained execution output requires its original reference")
        if result.output_ref is not None and result.output_ref.execution_id != result.execution_id:
            raise ValueError("execution output reference differs from receipt identity")
        return result

    @classmethod
    def from_shell_result(cls, payload: Mapping[str, object]) -> ExecutionReceipt:
        """Interpret the immediate shell runner's outcome at the write boundary."""
        code = payload.get("exit_code")
        if code is not None and type(code) is not int:
            raise ValueError("execution exit_code must be an integer or null")
        output = require_json_string(payload.get("output", ""), "output")
        error = payload.get("error")
        if error is not None:
            error = require_json_string(error, "error")
        scope = payload.get("exit_code_scope", "shell")
        if scope != "shell" and scope != "process":
            raise ValueError("invalid exit code scope")
        return cls(
            status=require_json_string(payload.get("status", "unknown"), "status"),
            exit_code=code, output=output or str(error or ""),
            truncated=require_json_boolean(payload.get("truncated", False), "truncated"),
            capture_truncated=require_json_boolean(payload.get("capture_truncated", False), "capture_truncated"),
            execution_id=require_json_string(payload.get("execution_id", ""), "execution_id"),
            exit_code_scope="process" if scope == "process" else "shell",
            full_output_retained=require_json_boolean(payload.get("full_output_retained", False), "full_output_retained"),
            output_ref=ExecutionOutputReference.from_payload(payload["output_ref"]) if payload.get("output_ref") is not None else None,
        )

    @property
    def full_output_recoverable(self) -> bool:
        return ((self.status == "exit" or self.full_output_retained) and not self.capture_truncated
                and (not self.truncated or self.full_output_retained))

    def facts(self) -> JsonObject:
        return {"status": self.status, "exit_code": self.exit_code, "exit_code_scope": self.exit_code_scope,
                "truncated": self.truncated, "capture_truncated": self.capture_truncated,
                "execution_id": self.execution_id, "full_output_retained": self.full_output_retained,
                "full_output_recoverable": self.full_output_recoverable,
                "output_ref": self.output_ref.facts() if self.output_ref is not None else None}

    def audit_facts(self) -> JsonObject:
        """Diagnostic projection of the recorded execution."""
        return {"exit_code": self.exit_code, "exit_code_scope": self.exit_code_scope,
                "execution_id": self.execution_id, "capture_truncated": self.capture_truncated,
                "full_output_retained": self.full_output_retained}

    def _header(self) -> str:
        header = (f"Execution status: {self.status}\n"
                  f"Exit code: {self.exit_code if self.exit_code is not None else 'unknown'}\n"
                  f"Exit code scope: {self.exit_code_scope}\n"
                  f"Output truncated: {str(self.truncated).lower()}\n"
                  f"Capture truncated: {str(self.capture_truncated).lower()}\n"
                  f"Full execution output recoverable: {str(self.full_output_recoverable).lower()}\n")
        if self.execution_id:
            header += f"Execution ID: {self.execution_id}\n"
        return header + "Available receipt views: output\n\nOutput:\n"

    def bounded(self, *, limit: int = 24_000) -> ExecutionReceipt:
        body, clipped = clip_middle(self.output or "(no output)", max(0, limit - len(self._header())))
        return replace(self, output=body, truncated=self.truncated or clipped)

    def render(self) -> str:
        return self._header() + self.output


def execution_receipt(result: ToolResult) -> ExecutionReceipt | None:
    """Read execution facts committed by the executor."""
    if result.call.name not in {"shell", "run"} or "execution" not in result.canonical:
        return None
    execution = result.canonical["execution"]
    if not isinstance(execution, dict):
        raise ValueError("canonical execution facts must be an object")
    return ExecutionReceipt.from_facts(execution)
