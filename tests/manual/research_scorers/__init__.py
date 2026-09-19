"""Shared research scorers for manual experiment harnesses only.

These helpers score follow-up usefulness and source-finalizer rows for
A/B transcripts. They are intentionally outside ``codey/``: no production
runtime imports them. Manual harnesses and their gate tests import from
here instead of ``codey.research``.
"""

from tests.manual.research_scorers.followup_quality import (
    answer_status_rank,
    followup_usefulness,
    score_followup_quality_row,
)
from tests.manual.research_scorers.source_finalizer_scoring import (
    aggregate_source_finalizer_rows,
    average_deltas,
    average_rows,
    paired_source_finalizer_deltas,
    rate_rows,
    score_source_finalizer_row,
)

__all__ = [
    "aggregate_source_finalizer_rows",
    "answer_status_rank",
    "average_deltas",
    "average_rows",
    "followup_usefulness",
    "paired_source_finalizer_deltas",
    "rate_rows",
    "score_followup_quality_row",
    "score_source_finalizer_row",
]
