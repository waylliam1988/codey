"""Operation mode result values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModeOutcome:
    event: dict
    research_result: Any | None = None
    research_pipeline_result: Any | None = None

