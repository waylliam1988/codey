from __future__ import annotations

import pytest

from codey.operations.recovery import _reconcile_delivery_witnesses
from codey.runtime.effects.tool_result_delivery import DeliveryCommit, ToolResultDeliveryError


def test_delivery_commit_round_trips_protocol_binding_and_digest() -> None:
    witness = DeliveryCommit(
        window_ref={"session_id": "s", "window_id": "w", "binding": "b"},
        batch_id="batch-1",
        provider_effect_id="effect-1",
        results_digest="a" * 32,
        invocation_kind="shell_approval",
        reply_status="accepted",
    )
    assert DeliveryCommit.from_payload(witness.to_payload()) == witness


def test_delivery_commit_rejects_missing_binding_fields() -> None:
    with pytest.raises(ToolResultDeliveryError):
        DeliveryCommit.from_payload({"batch_id": "batch-1"})


def test_recovery_reconciles_committed_witness_without_provider_send() -> None:
    witness = DeliveryCommit(
        window_ref={"session_id": "s", "window_id": "w", "binding": "b"},
        batch_id="batch-1", provider_effect_id="effect-1", results_digest="a" * 32,
        invocation_kind="native_tool_results", reply_status="accepted",
    ).to_payload()
    batch = type("Batch", (), {
        "intent": type("Intent", (), {"batch_id": "batch-1"})(),
        "is_delivered": False, "send_attempts": ("effect-1",),
    })()
    calls: list[tuple[str, str]] = []
    deps = type("Deps", (), {
        "context_ledger": type("Ledger", (), {
            "window_ref": type("Ref", (), {"to_payload": lambda self: witness["window_ref"]})(),
            "unreconciled_delivery_commits": lambda self, _ids: (witness,),
        })(),
        "runtime_mutations": type("Mutations", (), {
            "record_delivery_delivered": lambda self, session, run, **kw: calls.append((session, kw["batch_id"])),
        })(),
        "tool_result_delivery": type("Store", (), {
            "load_batches": lambda self, session, run: (batch,),
        })(),
    })()
    _reconcile_delivery_witnesses(deps, session_id="s", run_id="r")
    assert calls == [("s", "batch-1")]


def test_recovery_ignores_witness_from_another_run() -> None:
    stale = DeliveryCommit(
        window_ref={"session_id": "s", "window_id": "w", "binding": "b"},
        batch_id="deliv-old-run-t1-old", provider_effect_id="old-effect",
        results_digest="a" * 32, invocation_kind="native_tool_results",
        reply_status="accepted",
    ).to_payload()
    current = DeliveryCommit(
        window_ref={"session_id": "s", "window_id": "w", "binding": "b"},
        batch_id="deliv-current-run-t1-current", provider_effect_id="current-effect",
        results_digest="b" * 32, invocation_kind="native_tool_results",
        reply_status="accepted",
    ).to_payload()
    batches = (
        type("Batch", (), {
            "intent": type("Intent", (), {"batch_id": current["batch_id"]})(),
            "is_delivered": False,
            "send_attempts": (current["provider_effect_id"],),
        })(),
    )
    calls: list[tuple[str, str]] = []
    deps = type("Deps", (), {
        "context_ledger": type("Ledger", (), {
            "window_ref": type("Ref", (), {"to_payload": lambda self: current["window_ref"]})(),
            "unreconciled_delivery_commits": lambda self, _ids: (stale, current),
        })(),
        "runtime_mutations": type("Mutations", (), {
            "record_delivery_delivered": lambda self, session, run, **kw: calls.append((session, kw["batch_id"])),
        })(),
        "tool_result_delivery": type("Store", (), {
            "load_batches": lambda self, session, run: batches,
        })(),
    })()

    _reconcile_delivery_witnesses(deps, session_id="s", run_id="current-run")

    assert calls == [("s", current["batch_id"])]
