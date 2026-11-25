"""Exhaustive finite-state checks of real receipt and capability transitions.

The delivery graph fixes one safe batch and two provider-effect identities.
BFS reaches a fixed point (all reachable states, not a trace depth cutoff).
This supports induction inside that finite abstraction only. Durable crash
traces are checked separately in test_recovery_trace_invariants.py.
"""
from collections import deque
from collections.abc import Callable
from functools import partial
from itertools import product

from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.capabilities import KNOWN_TASK_GRANTS
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import tools_from_specs
from codey.runtime.effects.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    DeliveryBatchProjection,
    ToolResultDeliveryError,
    compute_batch_digest,
    mark_abandoned,
    mark_delivered,
    mark_recovered,
    mark_send_attempt,
    mark_send_superseded,
)


def test_delivery_graph_fixed_point_preserves_safety() -> None:
    items = (DeliveryBatchItem(0, "read", "effect", "safe"),)
    intent = DeliveryBatchIntent("batch", "s", "r", 1, items, compute_batch_digest(items))
    queue = deque([DeliveryBatchProjection(intent)])
    seen: set[DeliveryBatchProjection] = set()
    checked_edges = 0
    while queue:
        state = queue.popleft()
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
        def successors(state: DeliveryBatchProjection) -> tuple[DeliveryBatchProjection | None, ...]:
            def attempt(action: Callable[[], DeliveryBatchProjection | None]) -> DeliveryBatchProjection | None:
                try:
                    return action()
                except ToolResultDeliveryError:
                    return None

            batches = (state,)
            actions = [
                attempt(lambda: mark_recovered(batch_id="batch", batches=batches,
                    recovered_effect_ids=("effect",), recovered_reads=1)),
                attempt(lambda: mark_abandoned(batch_id="batch", batches=batches, reason="writer_settled")),
            ]
            for mark in (mark_send_attempt, mark_send_superseded, mark_delivered):
                for identity in ("p1", "p2"):
                    actions.append(attempt(partial(mark, batch_id="batch", batches=batches, provider_effect_id=identity)))
            return tuple(actions)

        for next_state in successors(state):
            if next_state is None:
                continue
            checked_edges += 1
            if state.is_terminal:
                assert next_state.is_terminal
                assert next_state.is_delivered == state.is_delivered
                assert next_state.is_abandoned == state.is_abandoned
            if next_state not in seen:
                queue.append(next_state)
    # Coverage guards: the traversal includes recovery, retries and both ends.
    assert any(s.is_recovered and not s.is_terminal for s in seen)
    assert any(s.is_recovered and s.is_delivered for s in seen)
    assert any(s.is_recovered and s.is_abandoned for s in seen)
    assert any(len(s.send_attempts) == 2 for s in seen)
    print(f"delivery fixed point: {len(seen)} states, {checked_edges} accepted edges")


def test_every_capability_subset_has_same_json_native_boundary_and_exact_roundtrip() -> None:
    vocabulary = tuple(sorted(KNOWN_TASK_GRANTS))
    for bits in product((False, True), repeat=len(vocabulary)):
        grants = frozenset(grant for grant, included in zip(vocabulary, bits, strict=True) if included)
        for denied in (frozenset(), grants):
            policy = TaskPolicy(grants, denied_capabilities=denied)
            assert TaskPolicy.from_payload(policy.to_payload()) == policy
            snapshot = build_turn_snapshot(TaskSession(policy=policy), native=True)
            assert all(spec.grant in grants - denied for spec in snapshot.frozen_specs)
            native_names = {row.name for row in tools_from_specs(snapshot.model_specs)}
            assert native_names == set(snapshot.tool_names)
            assert {spec.name for spec in snapshot.frozen_specs} == set(snapshot.inventory_names)
            assert set(snapshot.nested_tool_names) <= set(snapshot.inventory_names)
    print(f"capability enumeration: {2 ** len(vocabulary)} subsets x 2 denial states")
