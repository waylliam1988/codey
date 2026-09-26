"""Regression lock for work_queue transition helpers (pure extraction)."""

from __future__ import annotations

from codey.ghost import work_queue as wq

TS = "2026-01-01T00:00:00Z"
NOW = "2026-01-02T00:00:00Z"


def _item(status="queued", kind="coding", retry=0, run_id=""):
    return wq._new_item(
        kind=kind,
        status=status,
        scope="user",
        scope_ref="",
        title="Fix provider retry logic",
        why_now="Needs follow up soon",
        priority=0.5,
        confidence=0.5,
        source="user",
        source_ref="src-1",
        evidence_refs=(),
        run_refs=(),
        now=TS,
        metadata={},
    )


def _running_item(run_id="run-1", retry=0, kind="coding"):
    base = _item(status="queued", kind=kind, retry=retry)
    from dataclasses import replace

    return replace(
        base,
        status="running",
        started_run_id=run_id,
        retry_count=retry,
        lease_expires_at="2026-06-01T00:00:00Z",
    )


def _event(action, item, patch):
    return {
        "item_id": item.id,
        "action": action,
        "precondition": {
            "expected_status": item.status,
            "expected_started_run_id": item.started_run_id,
            "expected_retry_count": item.retry_count,
        },
        "patch": patch,
    }


def test_valid_claim_ok():
    item = _item(status="queued", retry=0)
    ev = _event(
        "claim",
        item,
        {
            "status": "running",
            "started_run_id": "run-1",
            "retry_count": 1,
            "lease_expires_at": "2026-06-01T00:00:00Z",
            "updated_at": TS,
        },
    )
    assert wq._valid_work_transition(ev) is True
    assert wq._valid_claim_transition(
        ev["patch"], "running", "queued", "", 0
    ) is True


def test_valid_claim_bad_status():
    item = _item(status="queued", retry=0)
    ev = _event(
        "claim",
        item,
        {
            "status": "queued",
            "started_run_id": "run-1",
            "retry_count": 1,
            "lease_expires_at": "2026-06-01T00:00:00Z",
            "updated_at": TS,
        },
    )
    assert wq._valid_work_transition(ev) is False


def test_valid_complete_ok():
    item = _running_item(run_id="run-1")
    ev = _event(
        "complete",
        item,
        {
            "status": "done",
            "completed_run_id": "run-1",
            "proof_refs": ["diff:run-1"],
            "updated_at": TS,
        },
    )
    assert wq._valid_work_transition(ev) is True


def test_valid_complete_missing_proof():
    item = _running_item(run_id="run-1")
    ev = _event(
        "complete",
        item,
        {"status": "done", "completed_run_id": "run-1", "updated_at": TS},
    )
    assert wq._valid_work_transition(ev) is False


def test_valid_release_queued_ok():
    item = _running_item(run_id="run-1")
    ev = _event(
        "release",
        item,
        {"status": "queued", "updated_at": TS},
    )
    assert wq._valid_work_transition(ev) is True
    assert wq._valid_release_transition(ev["patch"], "queued", "running", "run-1") is True


def test_valid_release_stale_blocked_ok():
    item = _running_item(run_id="run-1")
    ev = _event(
        "release_stale",
        item,
        {"status": "blocked", "blocked_reason": "stale_claim", "updated_at": TS},
    )
    assert wq._valid_work_transition(ev) is True


def test_valid_block_ok():
    item = _item(status="queued")
    ev = _event(
        "block",
        item,
        {"status": "blocked", "blocked_reason": "need info", "updated_at": TS},
    )
    assert wq._valid_work_transition(ev) is True


def test_valid_reject_ok():
    item = _item(status="candidate")
    ev = _event(
        "reject",
        item,
        {"status": "rejected", "updated_at": TS},
    )
    assert wq._valid_work_transition(ev) is True


def test_valid_queue_ok():
    base = _item(status="candidate")
    from dataclasses import replace

    item = replace(base, status="blocked", blocked_reason="x")
    ev = _event(
        "queue",
        item,
        {"status": "queued", "retry_count": 0, "updated_at": TS},
    )
    assert wq._valid_work_transition(ev) is True


def test_valid_queue_bad_retry():
    item = _item(status="candidate")
    ev = _event(
        "queue",
        item,
        {"status": "queued", "retry_count": 1, "updated_at": TS},
    )
    assert wq._valid_work_transition(ev) is False


def test_apply_claim_ok():
    item = _item(status="queued", retry=0)
    by = {item.id: item}
    ev = _event(
        "claim",
        item,
        {
            "status": "running",
            "started_run_id": "run-9",
            "retry_count": 1,
            "lease_expires_at": "2026-06-01T00:00:00Z",
            "updated_at": NOW,
        },
    )
    assert wq._apply_transition_event(by, ev, now=NOW) == "applied"
    assert by[item.id].status == "running"
    assert by[item.id].started_run_id == "run-9"


def test_apply_complete_ok():
    item = _running_item(run_id="run-1", kind="coding")
    by = {item.id: item}
    ev = _event(
        "complete",
        item,
        {
            "status": "done",
            "completed_run_id": "run-1",
            "proof_refs": ["diff:run-1"],
            "updated_at": NOW,
        },
    )
    assert wq._apply_transition_event(by, ev, now=NOW) == "applied"
    assert by[item.id].status == "done"


def test_apply_release_to_queued():
    item = _running_item(run_id="run-1")
    by = {item.id: item}
    ev = _event("release", item, {"status": "queued", "updated_at": NOW})
    assert wq._apply_transition_event(by, ev, now=NOW) == "applied"
    assert by[item.id].status == "queued"
    assert by[item.id].started_run_id == ""


def test_apply_release_to_blocked():
    item = _running_item(run_id="run-1")
    by = {item.id: item}
    ev = _event(
        "release_stale",
        item,
        {"status": "blocked", "blocked_reason": "stale_claim", "updated_at": NOW},
    )
    assert wq._apply_transition_event(by, ev, now=NOW) == "applied"
    assert by[item.id].status == "blocked"


def test_apply_block_ok():
    item = _item(status="queued")
    by = {item.id: item}
    ev = _event(
        "block", item, {"status": "blocked", "blocked_reason": "dep", "updated_at": NOW}
    )
    assert wq._apply_transition_event(by, ev, now=NOW) == "applied"
    assert by[item.id].status == "blocked"


def test_apply_reject_ok():
    item = _item(status="candidate")
    by = {item.id: item}
    ev = _event("reject", item, {"status": "rejected", "updated_at": NOW})
    assert wq._apply_transition_event(by, ev, now=NOW) == "applied"
    assert by[item.id].status == "rejected"


def test_apply_queue_ok():
    from dataclasses import replace

    base = _item(status="candidate")
    item = replace(base, status="blocked", blocked_reason="x")
    by = {item.id: item}
    ev = _event(
        "queue", item, {"status": "queued", "retry_count": 0, "updated_at": NOW}
    )
    assert wq._apply_transition_event(by, ev, now=NOW) == "applied"
    assert by[item.id].status == "queued"
    assert by[item.id].retry_count == 0


def test_apply_stale_and_invalid():
    item = _item(status="queued")
    assert wq._apply_transition_event({"other": item}, _event("claim", item, {}), now=NOW) == "stale"
    by = {item.id: item}
    bad = _event("claim", item, {"status": "running", "updated_at": NOW})
    # missing started_run_id/retry -> invalid (precondition ok, matrix ok)
    bad["precondition"] = {
        "expected_status": "queued",
        "expected_started_run_id": "",
        "expected_retry_count": 0,
    }
    assert wq._apply_transition_event(by, bad, now=NOW) == "invalid"


def test_helpers_direct():
    item = _item(status="queued", retry=0)
    assert wq._apply_claim_transition(item, {"status": "x"}, now=NOW) is None
    assert wq._apply_block_transition(item, {"blocked_reason": ""}, now=NOW) is None
    assert wq._apply_queue_transition(item, {}, now=NOW) is None
    updated = wq._apply_reject_transition(item, {"updated_at": NOW}, now=NOW)
    assert updated is not None and updated.status == "rejected"
