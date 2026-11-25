"""Research inputs captured for the model and checked against its tool results."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from codey.types.json_value import JsonObject, decode_json_object, require_json_strings

if TYPE_CHECKING:
    from codey.research.answer_contract import AnswerScope


@dataclass(frozen=True)
class ResearchTurnInputs:
    scope: AnswerScope | None = None
    data_file_reads: tuple[str, ...] | None = None

    def to_payload(self) -> JsonObject:
        return {
            "research_scope": decode_json_object(self.scope.to_payload()) if self.scope is not None else None,
            "data_file_reads": None if self.data_file_reads is None else list(self.data_file_reads),
        }

    @classmethod
    def from_payload(cls, payload: JsonObject) -> ResearchTurnInputs:
        from codey.research.answer_contract import AnswerScope

        scope, files = payload["research_scope"], payload["data_file_reads"]
        return cls(AnswerScope.from_payload(scope) if scope is not None else None,
                   tuple(require_json_strings(files, "data file reads")) if files is not None else None)
