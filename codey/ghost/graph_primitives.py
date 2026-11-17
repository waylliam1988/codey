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
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Protocol, TypeVar, runtime_checkable


class GraphRow(Protocol):
    @property
    def weight(self) -> float: ...

    @property
    def updated_at(self) -> str: ...


@runtime_checkable
class GraphNode(GraphRow, Protocol):
    @property
    def status(self) -> str: ...


class GraphEdge(GraphRow, Protocol):
    @property
    def source(self) -> str: ...

    @property
    def target(self) -> str: ...


_NodeT = TypeVar("_NodeT", bound=GraphNode)
_EdgeT = TypeVar("_EdgeT", bound=GraphEdge)


def parse_ts(value: object) -> datetime:
    """Parse recorded ISO time; legacy naive timestamps mean UTC.

    Missing or corrupt history is an error, never the current wall clock.
    Callers own rollback and reporting at the projection boundary.
    """
    if not isinstance(value, str):
        raise ValueError("invalid Ghost timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ValueError("invalid Ghost timestamp") from None


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
    age = (parse_ts(now) - parse_ts(basis)).total_seconds()
    return float(weight or 0.0) * exp_decay_factor(age, half_life_days)


def any_decay_due(
    bases: Iterable[str],
    *,
    now: str,
    min_interval_seconds: int,
) -> bool:
    """True when any decay basis is older than the minimum interval."""
    threshold = max(0, (min_interval_seconds or 0))
    now_ts = parse_ts(now)
    if threshold <= 0:
        return True
    for basis in bases:
        age = (now_ts - parse_ts(basis)).total_seconds()
        if max(0.0, age) >= threshold:
            return True
    return False


def _node_rank(row: GraphNode) -> tuple[bool, float, str]:
    return (
        row.status == "active",
        float(row.weight or 0.0),
        row.updated_at,
    )


def _active_row(row: GraphRow) -> bool:
    return isinstance(row, GraphNode) and row.status == "active"


def bound_graph_nodes(
    rows: Iterable[_NodeT],
    *,
    min_active_weight: float,
    limit: int,
) -> list[_NodeT]:
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
        if row.status != "active" or float(row.weight or 0.0) >= threshold
    ]
    kept.sort(key=_node_rank, reverse=True)
    return kept[: max(0, (limit or 0))]


def bound_graph_edges(
    rows: Iterable[_EdgeT],
    *,
    node_ids: set[str],
    min_weight: float,
    limit: int,
    max_out_degree: int,
    require_active: bool = False,
) -> list[_EdgeT]:
    """Keep edges between surviving nodes above ``min_weight`` (active-only
    when ``require_active``), by weight, with per-endpoint degree caps."""
    threshold = float(min_weight)
    cap = max(0, (limit or 0))
    degree_cap = max(0, (max_out_degree or 0))
    kept = [
        row
        for row in rows
        if row.source in node_ids
        and row.target in node_ids
        and float(row.weight or 0.0) >= threshold
        and (not require_active or _active_row(row))
    ]
    kept.sort(
        key=lambda row: (
            float(row.weight or 0.0),
            row.updated_at,
        ),
        reverse=True,
    )
    bounded: list[_EdgeT] = []
    degree: dict[str, int] = {}
    for row in kept:
        if len(bounded) >= cap:
            break
        source = row.source
        target = row.target
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
    "parse_ts",
]
