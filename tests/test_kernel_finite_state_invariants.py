"""Exhaustive finite-state checks of real receipt and capability transitions.

The delivery graph fixes one safe batch and two provider-effect identities.
BFS reaches a fixed point (all reachable states, not a trace depth cutoff).
This supports induction inside that finite abstraction only. Durable crash
traces are checked separately in test_recovery_trace_invariants.py.
"""
from collections import deque
from itertools import product

from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.capabilities import KNOWN_TASK_GRANTS
from codey.policies.task_policy import TaskPolicy
from codey.runtime.effects.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    ToolResultDeliveryError,
    abandoned_entry,
    batch_intent_entry,
    batches_from_entries,
    compute_batch_digest,
    delivered_entry,
    recovered_entry,
    send_attempt_entry,
    send_superseded_entry,
)
from codey.runtime.log.entries import RuntimeLogEntry


def _log_row(entry):
    return RuntimeLogEntry(session_id="s", lane=entry["lane"], operation_id=entry["operation_id"],
                           kind=entry["kind"], payload=entry["payload"])


def test_delivery_graph_fixed_point_preserves_safety():
    items = (DeliveryBatchItem(0, "read", "effect", "safe"),)
    intent = DeliveryBatchIntent("batch", "s", "r", 1, items, compute_batch_digest(items))
    initial = (_log_row(batch_intent_entry(intent)),)
    queue = deque([initial])
    seen = set()
    checked_edges = 0
    while queue:
        rows = queue.popleft()
        batches = batches_from_entries(rows, session_id="s", run_id="r")
        state = batches[0]
        if state in seen:
            continue
        seen.add(state)
        assert not (state.is_delivered and state.is_abandoned)
        assert state.is_delivered is bool(state.delivered_effect_ids)
        assert len(state.delivered_effect_ids) <= 1
        assert set(state.delivered_effect_ids) <= set(state.send_attempts) - set(state.superseded_effect_ids)
        assert len(state.active_attempts) <= 1
        assert state.intent.tool_refs == ("effect",)
        assert state.is_terminal is (state.is_delivered or state.is_abandoned)
        assert state.can_recover_before_provider_send is (not state.is_terminal and not state.active_attempts)
        actions = [
            (recovered_entry, {"recovered_effect_ids": ["effect"], "recovered_reads": 1}),
            (abandoned_entry, {"reason": "writer_settled"}),
        ]
        actions.extend((fn, {"provider_effect_id": identity})
                       for fn in (send_attempt_entry, send_superseded_entry, delivered_entry)
                       for identity in ("p1", "p2"))
        for fn, args in actions:
            try:
                entry = fn("s", "r", batch_id="batch", batches=batches, **args)
            except ToolResultDeliveryError:
                continue  # An explicitly refused transition has no effects.
            checked_edges += 1
            following = rows if entry is None else (*rows, _log_row(entry))
            next_state = batches_from_entries(following, session_id="s", run_id="r")[0]
            if state.is_terminal:
                assert next_state.is_terminal
                assert next_state.is_delivered == state.is_delivered
                assert next_state.is_abandoned == state.is_abandoned
            if next_state not in seen:
                queue.append(following)
    # Coverage guards: the traversal includes recovery, retries and both ends.
    assert any(s.is_recovered and not s.is_terminal for s in seen)
    assert any(s.is_recovered and s.is_delivered for s in seen)
    assert any(s.is_recovered and s.is_abandoned for s in seen)
    assert any(len(s.send_attempts) == 2 for s in seen)
    print(f"delivery fixed point: {len(seen)} states, {checked_edges} accepted edges")


def test_every_capability_subset_has_same_json_native_boundary_and_exact_roundtrip():
    vocabulary = tuple(sorted(KNOWN_TASK_GRANTS))
    for bits in product((False, True), repeat=len(vocabulary)):
        grants = frozenset(grant for grant, included in zip(vocabulary, bits, strict=True) if included)
        for denied in (frozenset(), grants):
            policy = TaskPolicy(grants, denied_capabilities=denied)
            assert TaskPolicy.from_payload(policy.to_payload()) == policy
            snapshot = build_turn_snapshot(TaskSession(policy=policy), native=True)
            assert all(spec.grant in grants - denied for spec in snapshot.frozen_specs)
            native_names = {row["function"]["name"] for row in snapshot.native_tools}
            assert native_names == set(snapshot.tool_names) - {"parallel", "read_files"}
    print(f"capability enumeration: {2 ** len(vocabulary)} subsets x 2 denial states")
