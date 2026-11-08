"""Execution facts shared by model delivery and approval reconstruction."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal

from codey.types.json_value import JsonObject, require_json_boolean, require_json_string
from codey.utils.text_budget import clip_middle


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

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ExecutionReceipt:
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
        )

    @property
    def full_output_recoverable(self) -> bool:
        return self.status == "exit" and not self.capture_truncated and (not self.truncated or self.full_output_retained)

    def facts(self) -> JsonObject:
        return {"status": self.status, "exit_code": self.exit_code, "exit_code_scope": self.exit_code_scope,
                "truncated": self.truncated, "capture_truncated": self.capture_truncated,
                "execution_id": self.execution_id, "full_output_retained": self.full_output_retained,
                "full_output_recoverable": self.full_output_recoverable}

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
