"""Immutable input and identities for one owned tool operation."""
from __future__ import annotations

from dataclasses import dataclass

from codey.types.json_value import (
    JsonObject,
    canonical_json_object,
    require_json_integer,
    require_json_string,
)
from codey.utils.refs import stable_ref


def tool_task_id(origin_effect_id: str) -> str:
    return stable_ref("tool_task", origin_effect_id)


def execution_effect_id(task_id: str) -> str:
    return stable_ref("tool_execution", task_id)


@dataclass(frozen=True)
class ToolTaskInput:
    origin_effect_id: str
    call_name: str
    args: JsonObject
    snapshot: JsonObject
    project: str
    permission_profile: str
    turn: int
    tool_index: int
    deadline: float | None = None

    def to_payload(self) -> JsonObject:
        return canonical_json_object({
            "schema_version": 1, "origin_effect_id": self.origin_effect_id,
            "call_name": self.call_name, "args": self.args, "snapshot": self.snapshot,
            "project": self.project, "permission_profile": self.permission_profile,
            "turn": self.turn, "tool_index": self.tool_index, "deadline": self.deadline,
        })

    @classmethod
    def from_payload(cls, value: object) -> ToolTaskInput:
        row = canonical_json_object(value)
        if (set(row) != {"schema_version", "origin_effect_id", "call_name", "args", "snapshot", "project",
                         "permission_profile", "turn", "tool_index", "deadline"}
                or type(row["schema_version"]) is not int or row["schema_version"] != 1):
            raise ValueError("invalid tool task input version/fields")
        deadline = row["deadline"]
        if deadline is not None and (not isinstance(deadline, (float, int)) or isinstance(deadline, bool)):
            raise ValueError("invalid tool task deadline")
        result = cls(
            require_json_string(row["origin_effect_id"], "origin_effect_id"),
            require_json_string(row["call_name"], "call_name"),
            canonical_json_object(row["args"]), canonical_json_object(row["snapshot"]),
            require_json_string(row["project"], "project"),
            require_json_string(row["permission_profile"], "permission_profile"),
            require_json_integer(row["turn"], "turn"), require_json_integer(row["tool_index"], "tool_index"),
            float(deadline) if deadline is not None else None,
        )
        if (not result.origin_effect_id or result.call_name != "run" or not result.project
                or result.turn < 0 or result.tool_index < 0 or "run_in_background" in result.args):
            raise ValueError("invalid tool task operation")
        return result
