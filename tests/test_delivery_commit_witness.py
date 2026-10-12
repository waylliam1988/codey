from __future__ import annotations

import pytest

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
