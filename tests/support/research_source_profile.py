"""Legacy source-profile aggregation retained for offline regression fixtures."""

from __future__ import annotations

from collections.abc import Iterable

from codey.research.source_trust import (
    SOURCE_CLASSES,
    TIER_STRONG,
    TIER_WEAK,
    SourceTrustProjection,
)


def evaluate_against_profile(
    projections: Iterable[SourceTrustProjection],
    *,
    floor_tier: int = TIER_WEAK,
) -> dict[str, object]:
    """Combine projections with an optional strictness floor.

    Pure aggregation: counts, per-source below-floor warnings, and bounded
    preference hints. Never removes sources; rows below the floor stay in the
    set with a warning attached to the evaluation, not deleted.
    """

    rows = [item for item in projections or () if isinstance(item, SourceTrustProjection)]
    counts: dict[str, int] = {}
    for cls in SOURCE_CLASSES:
        matched = sum(1 for item in rows if item.source_class == cls)
        if matched:
            counts[cls] = matched
    warnings: list[str] = []
    if len(rows) < 2:
        warnings.append("single_source")
    if rows and all(item.freshness in {"", "undated", "stale"} for item in rows):
        warnings.append("sources_stale_or_undated")
    if rows and not any(item.tier >= TIER_STRONG for item in rows):
        warnings.append("no_strong_source")
    below_floor = sum(1 for item in rows if item.tier < max(TIER_WEAK, floor_tier))
    if below_floor:
        warnings.append("sources_below_quality_threshold")
    weak = sum(1 for item in rows if item.tier == TIER_WEAK)
    if weak:
        warnings.append("weak_source_class_present")
    return {
        "count": len(rows),
        "class_counts": counts,
        "below_floor_count": below_floor,
        "warnings": warnings[:8],
    }
