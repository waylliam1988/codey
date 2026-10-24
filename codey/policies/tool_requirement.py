"""Tool-success predicates encoded in the existing immutable required checks."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PREFIX = "tool_succeeded:"
_NAME = re.compile(r"[a-z][a-z0-9_.]{0,63}\Z")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def argument_digest(value: object) -> str:
    """Type-preserving selectors; do not retain file bodies in the projection."""
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolSuccessRequirement:
    tool: str
    via: str = ""
    arguments_json: str = "{}"

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.tool) or (self.via and not _NAME.fullmatch(self.via)):
            raise ValueError("tool requirement names must be canonical tool names")
        args = json.loads(self.arguments_json)
        if not isinstance(args, dict):
            raise ValueError("tool requirement arguments must be an object")
        object.__setattr__(self, "arguments_json", _json(args))

    @property
    def declaration(self) -> str:
        if not self.via and self.arguments_json == "{}":
            return PREFIX + self.tool
        return PREFIX + _json({"tool": self.tool, "via": self.via, "args": json.loads(self.arguments_json)})

    @property
    def check_id(self) -> str:
        if not self.via and self.arguments_json == "{}":
            return self.declaration
        # Completion proofs contain bounded identifiers, never raw selectors.
        return PREFIX + hashlib.sha256(self.declaration.encode("utf-8")).hexdigest()[:32]

    @property
    def guidance(self) -> str:
        target = f"{self.tool} with arguments matching {self.arguments_json}"
        if self.via:
            return (f"Required execution: inside {self.via}, call tools.{target}. "
                    "The child call and its parent must both commit successfully. "
                    f"A separate top-level {self.tool} call does not satisfy this requirement.")
        return f"Required execution: {target} must commit successfully."

    def satisfied_by(self, records: Mapping[str, Any]) -> bool:
        expected = {key: argument_digest(value) for key, value in json.loads(self.arguments_json).items()}
        for identity, row in records.items():
            if not _succeeded(row, self.tool):
                continue
            digests = row.get("argument_digests", {})
            if not isinstance(digests, dict) or any(digests.get(key) != value for key, value in expected.items()):
                continue
            if not self.via or any(
                _succeeded(parent, self.via) and identity in parent.get("child_effect_ids", ())
                for parent in records.values()
            ):
                return True
        return False


def _succeeded(row: Any, name: str) -> bool:
    return (isinstance(row, dict) and row.get("name") == name and row.get("ok") is True
            and row.get("execution_disposition") != "denied_before_execution")


def require_tool_success(tool: str, *, via: str = "", arguments: Mapping[str, object] | None = None) -> str:
    """Declare exact argument selectors, optionally bound to a successful parent."""
    return ToolSuccessRequirement(tool, via, _json(dict(arguments or {}))).declaration


def parse_tool_success_check(check_id: str) -> ToolSuccessRequirement | None:
    if not check_id.startswith(PREFIX):
        return None
    expression = check_id.removeprefix(PREFIX)
    if _NAME.fullmatch(expression):
        return ToolSuccessRequirement(expression)
    data = json.loads(expression)
    if (not isinstance(data, dict) or set(data) != {"tool", "via", "args"}
            or not isinstance(data["tool"], str) or not isinstance(data["via"], str)):
        raise ValueError("invalid structured tool requirement")
    requirement = ToolSuccessRequirement(data["tool"], data["via"], _json(data["args"]))
    if requirement.declaration != check_id:
        raise ValueError("tool requirement must use its canonical check id")
    return requirement
