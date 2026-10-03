"""A persisted invalid counter must not become a valid lease claim."""

from copy import deepcopy
from dataclasses import replace

import pytest

from codey.ghost.work_queue_events import _apply_transition_event, _valid_work_event, items_from_events
from tests.test_work_queue_events_owns_replay import _item


def _claim(value):
    return {
        "schema_version": 1,
        "type": "ghost_work_item_transitioned",
        "event_id": "claim-1",
        "ts": "2026-10-03T00:00:00Z",
        "action": "claim",
        "item_id": "w1",
        "precondition": {"expected_status": "queued", "expected_started_run_id": "", "expected_retry_count": 0},
        "patch": {
            "status": "running",
            "started_run_id": "run-1",
            "retry_count": value,
            "lease_expires_at": "2026-10-03T00:10:00Z",
            "blocked_reason": "",
            "updated_at": "2026-10-03T00:00:00Z",
        },
    }


@pytest.mark.parametrize("value", ["1", 1.0, 1.9, True])
def test_invalid_claim_counter_is_rejected_by_validation_and_replay(value):
    item = replace(_item(), created_at="2026-10-03T00:00:00Z", updated_at="2026-10-03T00:00:00Z")
    event = _claim(value)
    assert _valid_work_event(event) is False
    state = {item.id: item}
    assert _apply_transition_event(state, event, now=event["ts"]) == "invalid"
    assert state == {item.id: item}
    observed = {
        "schema_version": 1,
        "type": "ghost_work_item_observed",
        "event_id": "observed-1",
        "ts": event["ts"],
        "item": item.to_payload(),
    }
    assert items_from_events([observed, event]) == [item]


def test_exact_integer_claim_still_replays():
    event = _claim(1)
    before = deepcopy(event)
    assert _valid_work_event(event) is True
    state = {"w1": _item()}
    assert _apply_transition_event(state, event, now=event["ts"]) == "applied"
    assert state["w1"].status == "running"
    assert state["w1"].retry_count == 1
    assert event == before
