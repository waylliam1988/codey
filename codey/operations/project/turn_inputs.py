"""Project evidence captured before a model turn and restored from its request."""
from __future__ import annotations

from dataclasses import dataclass

from codey.operations.project.project_instructions import ProjectInstructionBinding
from codey.types.json_value import JsonObject, decode_json_object, require_json_string


@dataclass(frozen=True)
class ProjectTurnInputs:
    instructions: tuple[ProjectInstructionBinding, ...] = ()
    symbol_reads: tuple[tuple[str, str], ...] = ()

    def to_payload(self) -> JsonObject:
        return {"project_instructions": [rule.to_payload() for rule in self.instructions],
                "symbol_read_refs": dict(self.symbol_reads)}

    @classmethod
    def from_payload(cls, payload: JsonObject, *, allow_write: bool) -> ProjectTurnInputs:
        raw_instructions = payload["project_instructions"]
        if not isinstance(raw_instructions, list):
            raise ValueError("invalid captured project instruction collection")
        instructions = tuple(ProjectInstructionBinding.from_payload(rule) for rule in raw_instructions)
        if len({rule.name for rule in instructions}) != len(instructions) or instructions and not allow_write:
            raise ValueError("invalid captured project instruction scope")
        symbol_reads = tuple(sorted(
            (path, require_json_string(ref, "symbol read receipt"))
            for path, ref in decode_json_object(payload["symbol_read_refs"]).items()
        ))
        return cls(instructions, symbol_reads)
