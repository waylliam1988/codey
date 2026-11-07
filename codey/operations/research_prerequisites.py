"""Historical prerequisites are facts, never the current acquisition session."""

from __future__ import annotations

from dataclasses import dataclass, replace

from codey.operations.task_session import TaskSession
from codey.runtime.core.execution_record import ExecutionFact, ExecutionRecord
from codey.runtime.core.models import ToolResult
from codey.types.json_value import decode_json_object


def detached_result(result: ToolResult) -> ToolResult:
    return replace(result, call=replace(result.call, args=decode_json_object(result.call.args)))


@dataclass(frozen=True)
class ResearchPrerequisiteFacts:
    executions: tuple[ExecutionFact, ...]
    _results: tuple[tuple[str, ToolResult], ...]

    @classmethod
    def capture(cls, session: TaskSession) -> ResearchPrerequisiteFacts:
        return cls(tuple(ExecutionFact.capture(key, row) for key, row in session.executed.items()),
                   tuple((key, detached_result(row)) for key, row in session._memory_results.items()))

    @property
    def executed(self) -> dict[str, ExecutionRecord]:
        return {row.identity: row.to_record() for row in self.executions}

    @property
    def results(self) -> dict[str, ToolResult]:
        return {key: detached_result(row) for key, row in self._results}
