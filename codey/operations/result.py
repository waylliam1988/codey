"""Operation mode result values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codey.research.pipeline import ResearchPipelineResult
    from codey.research.run_result import ResearchRunResult


@dataclass(frozen=True)
class ModeOutcome:
    event: dict[str, object]
    research_result: ResearchRunResult | None = None
    research_pipeline_result: ResearchPipelineResult | None = None
    # Final user-visible events (reply/review/...) published by settlement only,
    # after the task outcome is durably committed. Modes must not
    # emit these themselves.
    display: tuple[dict[str, object], ...] = ()

