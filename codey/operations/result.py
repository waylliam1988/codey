"""Operation mode result values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModeOutcome:
    event: dict
    research_result: Any | None = None
    research_pipeline_result: Any | None = None
    # Final user-visible events (reply/review/...) published by settlement only,
    # after the experience observation is durably committed. Modes must not
    # emit these themselves.
    display: tuple[dict[str, object], ...] = ()

