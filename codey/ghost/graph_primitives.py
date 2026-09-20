"""Shared time-decay math for Ghost graph stores.

Hebbian memory (``hebbian.py``) and the affinity scheduler projection
(``affinity.py``) are different layers -- a memory graph vs. a scheduling
preference index -- but they decay on the same half-life curve and parse the
same timestamp format. This module owns that pure math so a fix to the curve
lands once.

Deliberately *not* owned here:

- clamping: Hebbian rounds with ``_clamp01`` while affinity clamps with
  ``clamp_unit_float`` (different ``bool``/``NaN`` edges). Callers clamp the
  raw value this module returns.
- ref merging: ``_merge_refs`` differs per store on purpose (affinity
  filters sensitive/multiline refs; Hebbian does not). Do not unify.
- specs/projection: affinity's ``_specs_from_*`` layer stays in
  ``affinity.py``; it is the personality-facing preference projection, not
  storage mechanics.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable, TypeVar

_RowT = TypeVar("_RowT")


def parse_ts(value: object) -> datetime:
    """Parse an ISO timestamp; unparseable input means "now" (fail lively)."""
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def decay_basis_of(
    last_decayed_at: str,
    last_reinforced_at: str,
    updated_at: str,
) -> str:
    """Most recent decay anchor for a node/edge row."""
    return last_decayed_at or last_reinforced_at or updated_at


def exp_decay_factor(age_seconds: float, half_life_days: float) -> float:
    """Half-life decay factor for an age: ``0.5 ** (age / half_life)``."""
    half_life_seconds = max(1.0, float(half_life_days) * 24.0 * 60.0 * 60.0)
    decay_rate = math.log(2.0) / half_life_seconds
    return math.exp(-decay_rate * max(0.0, age_seconds))


def decayed_by_half_life(
    weight: float,
    basis: str,
    now: str,
    half_life_days: float,
) -> float:
    """Raw decayed weight (unclamped). Callers apply their own clamp."""
    try:
        age = (parse_ts(now) - parse_ts(basis)).total_seconds()
    except Exception:
        return float(weight or 0.0)
    return float(weight or 0.0) * exp_decay_factor(age, half_life_days)


def any_decay_due(
    bases: Iterable[str],
    *,
    now: str,
    min_interval_seconds: int,
) -> bool:
    """True when any decay basis is older than the minimum interval."""
    threshold = max(0, int(min_interval_seconds or 0))
    if threshold <= 0:
        return True
    now_ts = parse_ts(now)
    for basis in bases:
        try:
            age = (now_ts - parse_ts(basis)).total_seconds()
        except Exception:
            continue
        if max(0.0, age) >= threshold:
            return True
    return False


def _node_rank(row: Any) -> tuple[Any, ...]:
    return (
        getattr(row, "status", "") == "active",
        float(getattr(row, "weight", 0.0) or 0.0),
        str(getattr(row, "updated_at", "")),
    )


def bound_graph_nodes(
    rows: Iterable[_RowT],
    *,
    min_active_weight: float,
    limit: int,
) -> list[_RowT]:
    """Keep active rows above ``min_active_weight`` (inactive rows always
    survive the filter), active-first by weight, truncated to ``limit``.

    Row protocol: ``.status`` / ``.weight`` / ``.updated_at``. Both the
    Hebbian memory graph and the affinity preference index satisfy it with
    different thresholds; the ranking is shared.
    """
    threshold = float(min_active_weight)
    kept = [
        row
        for row in rows
        if getattr(row, "status", "") != "active"
        or float(getattr(row, "weight", 0.0) or 0.0) >= threshold
    ]
    kept.sort(key=_node_rank, reverse=True)
    return kept[: max(0, int(limit or 0))]


def bound_graph_edges(
    rows: Iterable[_RowT],
    *,
    node_ids: set[str],
    min_weight: float,
    limit: int,
    max_out_degree: int,
    require_active: bool = False,
) -> list[_RowT]:
    """Keep edges between surviving nodes above ``min_weight`` (active-only
    when ``require_active``), by weight, with per-endpoint degree caps."""
    threshold = float(min_weight)
    cap = max(0, int(limit or 0))
    degree_cap = max(0, int(max_out_degree or 0))
    kept = [
        row
        for row in rows
        if getattr(row, "source", None) in node_ids
        and getattr(row, "target", None) in node_ids
        and float(getattr(row, "weight", 0.0) or 0.0) >= threshold
        and (not require_active or getattr(row, "status", "") == "active")
    ]
    kept.sort(
        key=lambda row: (
            float(getattr(row, "weight", 0.0) or 0.0),
            str(getattr(row, "updated_at", "")),
        ),
        reverse=True,
    )
    bounded: list[_RowT] = []
    degree: dict[str, int] = {}
    for row in kept:
        if len(bounded) >= cap:
            break
        source = str(getattr(row, "source", ""))
        target = str(getattr(row, "target", ""))
        if degree.get(source, 0) >= degree_cap:
            continue
        if degree.get(target, 0) >= degree_cap:
            continue
        bounded.append(row)
        degree[source] = degree.get(source, 0) + 1
        degree[target] = degree.get(target, 0) + 1
    return bounded


__all__ = [
    "any_decay_due",
    "bound_graph_edges",
    "bound_graph_nodes",
    "decay_basis_of",
    "decayed_by_half_life",
    "exp_decay_factor",
    "now_iso",
    "parse_ts",
]
