"""Compaction retention policies: the leaf both sides share.

`compaction.py` needs these predicates, but importing them from
`effect_records`/`tool_result_delivery` pulls the whole effects layer into
the log package (session_log -> compaction -> effects -> session_log).
The predicates only compare status literals, so they live here with the
literals they need; the effects modules re-export the literals so existing
importers do not move.
"""

from __future__ import annotations


SETTLEMENT_STATUS_ERROR = "error"
SETTLEMENT_STATUS_INTERRUPTED = "interrupted"
SENT_STATE_MAYBE_SENT = "maybe_sent"
RECORD_KIND_RECOVERED = "recovered"


def keep_effect_pair_for_compaction(
    *,
    is_open: bool,
    settlement_payload: dict | None,
) -> bool:
    """Compaction retention policy for one intent/settlement pair.

    Open operations keep the pair so resume can replay or settle. Settled
    operations keep only pairs that explain an abnormal ending: interrupted
    or error status, maybe-sent provider calls, or replayed effects.
    """
    if is_open:
        return True
    if settlement_payload is None:
        return False
    replay_count = settlement_payload.get("replay_count")
    replayed = replay_count if isinstance(replay_count, int) and replay_count > 0 else 0
    return (
        settlement_payload.get("status") in {SETTLEMENT_STATUS_INTERRUPTED, SETTLEMENT_STATUS_ERROR}
        or settlement_payload.get("sent_state") == SENT_STATE_MAYBE_SENT
        or replayed > 0
    )


def keep_delivery_entry_for_compaction(*, record_kind: str, is_open: bool) -> bool:
    """Compaction retention policy for one delivery record.

    Open operations keep every delivery record so resume can finish the
    batch. Settled operations keep only recovered facts.
    """
    if is_open:
        return True
    return record_kind == RECORD_KIND_RECOVERED
