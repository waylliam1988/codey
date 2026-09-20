"""Shared projection-file mechanics for Ghost event-backed stores.

Covers exactly two things both the Hebbian memory graph and the affinity
preference index do byte-for-byte alike:

- reading and validating a persisted projection file
  (:func:`read_projection_payload`), and
- deciding whether an event log is over its compaction budget
  (:func:`over_compact_budget`).

Deliberately NOT unified here (verified by pairwise similarity scan of all
115 + 64 store functions; every remaining resemblance is store semantics in
similar clothes):

- corruption *consequences*: Hebbian quarantines its state file to
  ``state.json.quarantine.*`` (pinned by tests), affinity backs its
  projection up via ``backup_corrupt_file``. The reason strings below let
  each store keep its own consequence.
- row parsing: node/edge payload shapes, ref filtering, and clamps differ
  per store and stay local (see ``graph_primitives`` for what IS shared).
- write paths: Hebbian is append-first with rewrite fallback, affinity is
  replace-or-append that raises. A common mutate driver would need 3+
  callbacks longer than the driver itself -- framework for framework's sake.
- warnings streams, event validators, and snapshot envelopes are per-store
  by design (different ``source_name`` namespaces).

If you are tempted to merge the two loaders further, re-run the similarity
scan first: above ~0.8 everything is already shared atoms (``event_log``,
``local_store``, ``file_lock``, ``graph_primitives``).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from codey.storage.local_store import StoreCorruption, read_json_strict


def read_projection_payload(
    path: str | Path,
    *,
    schema_version: int,
    kind: str,
    max_bytes: int,
) -> tuple[dict[str, Any] | None, str]:
    """Read and validate a projection file without consequences.

    Returns ``(payload, reason)`` where reason is ``""`` on success,
    ``"missing"`` when the file is absent, ``"corrupt"`` when it cannot be
    trusted, or ``"wrong_schema"`` when version/kind drift. Callers apply
    their own corruption consequence (quarantine vs backup) and row parsing.
    """
    target = Path(path)
    if not target.exists():
        return None, "missing"
    try:
        payload = read_json_strict(target, max_bytes=max_bytes)
    except StoreCorruption:
        return None, "corrupt"
    if payload is None:
        # Raced deletion between the exists() check and the read.
        return None, "missing"
    if payload.get("schema_version") != schema_version or payload.get("kind") != kind:
        return None, "wrong_schema"
    return dict(payload), ""


def over_compact_budget(
    stats: Mapping[str, object],
    *,
    max_events: int,
    max_bytes: int,
) -> bool:
    """True when an ``event_file_stats`` reading exceeds either budget."""
    try:
        events = int(stats.get("events") or 0)
        size = int(stats.get("bytes") or 0)
    except (TypeError, ValueError):
        return True
    return events > max(0, int(max_events or 0)) or size > max(0, int(max_bytes or 0))


__all__ = [
    "over_compact_budget",
    "read_projection_payload",
]
