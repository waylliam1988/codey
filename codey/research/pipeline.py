"""Run one research writer and project its settled audit and knowledge changes."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from codey.research.context import ResearchContext
from codey.research.run_result import ResearchRunResult
from codey.research.tools import ResearchTools


class ResearchContinuationError(RuntimeError):
    """A committed research continuation cannot be safely reconstructed."""


@dataclass(frozen=True)
class ResearchIterationRun:
    result: ResearchRunResult
    tools: ResearchTools | None = None


class ResearchIterationRunner(Protocol):
    def __call__(
        self,
        *,
        task: str,
        max_turns: int,
        chat_handoff: str,
        search: object,
        topic_continuity_context: str = "",
        topic_continuity_payload: Mapping[str, object] | None = None,
    ) -> ResearchIterationRun:
        ...


@dataclass(frozen=True)
class ResearchPipelineResult:
    final_result: ResearchRunResult
    stop_reason: str = ""
    final_evidence_count: int = 0

    def to_payload(self) -> dict[str, object]:
        return {"stop_reason": self.stop_reason, "final_evidence_count": self.final_evidence_count}


class ResearchPipeline:
    def __init__(
        self, *, context: ResearchContext,
        run_iteration: ResearchIterationRunner, search_factory: Callable[[], object],
        research_changes_sink: Callable[[str, object, str], None] | None = None,
    ) -> None:
        self.context = context
        self.run_iteration = run_iteration
        self.search_factory = search_factory
        self.research_changes_sink = research_changes_sink

    def run(self) -> ResearchPipelineResult:
        search = self.search_factory()
        try:
            run = self.run_iteration(
                task=self.context.question,
                max_turns=self.context.max_turns,
                chat_handoff=self.context.chat_handoff,
                search=search,
                topic_continuity_context=self.context.topic_continuity_context,
                topic_continuity_payload=self.context.topic_continuity_payload,
            )
            result = run.result
            self.context.trace.record_result(result)
            if self.research_changes_sink is not None and run.tools is not None and self.context.run_id:
                self.research_changes_sink(self.context.run_id, run.tools.changes, self.context.session_id)
            output = ResearchPipelineResult(result, result.stop_reason,
                len(result.accepted_report.evidence_bindings) if result.accepted_report is not None else 0)
            self.context.trace.record_pipeline_result(output)
            return output
        finally:
            close = getattr(search, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()


__all__ = [
    "ResearchIterationRun",
    "ResearchIterationRunner",
    "ResearchPipeline",
    "ResearchPipelineResult",
]
