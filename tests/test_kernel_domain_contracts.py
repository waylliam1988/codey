"""Domain ownership and codec checks for the typed kernel request path."""

from dataclasses import replace

import pytest

from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelResult,
    result_from_payload,
)
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.kernel_request_builder import KernelRunRequestBuilder
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.provider_request_context import ProviderRequestContext
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import TextError, decode_text_turn
from codey.providers.transport import ProviderTransport
from tests.durable.test_provider_response_receipts import runtime


def test_builder_adapts_raw_provider_and_normalizes_single_sink_owner(tmp_path):
    durable, _, _, sink = runtime(tmp_path)
    try:
        request = KernelRunRequestBuilder(object(), "r", "task", "local", "work").build(
            observation=KernelObservationDeps(intent_sink=sink))
        assert isinstance(request.transport.provider, ProviderTransport)
        assert request.execution.intent_sink is sink
        assert request.observation.intent_sink is None
        changed = replace(request, execution=replace(request.execution, permission_profile="other"))
        assert changed.execution.intent_sink is sink
        assert changed.observation.intent_sink is None
        # Compatibility construction is converted once, even during a restart.
        replacement = KernelExecutionDeps(intent_sink=sink)
        assert replace(request, execution=replacement).execution is replacement
    finally:
        durable.close()


def test_frozen_context_roundtrip_uses_named_snapshot_and_authority():
    session = TaskSession(TaskPolicy(frozenset({"control"})))
    snapshot = build_turn_snapshot(session)
    original = ProviderRequestContext(2, snapshot, None, "context", (), effect_scope="task", work_ref="decision")
    restored = ProviderRequestContext.from_payload(original.to_payload())
    assert restored == original
    for key, value in (("logical_turn", True), ("logical_turn", 0), ("effect_scope", 1), ("close_reply", 0)):
        with pytest.raises(ValueError):
            ProviderRequestContext.from_payload({**original.to_payload(), key: value})


def test_snapshot_spec_decoder_rejects_wrong_primitive_types():
    snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control"}))))
    payload = snapshot_description(snapshot)
    payload["specs"][0]["parallel_safe"] = "false"
    with pytest.raises(ValueError, match="parallel_safe"):
        restore_snapshot(payload)


def test_frozen_context_requires_explicit_scope_even_when_unscoped():
    snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control"}))))
    payload = ProviderRequestContext(1, snapshot, None, "", ()).to_payload()
    # An explicitly empty legacy scope is legal; a missing authority field is
    # malformed and must not silently inherit today's request scope.
    assert ProviderRequestContext.from_payload(payload).effect_scope == ""
    del payload["effect_scope"]
    with pytest.raises(ValueError, match="effect_scope"):
        ProviderRequestContext.from_payload(payload)


@pytest.mark.parametrize("field,value", [
    ("aliases", "done"), ("required", [1]), ("executor", None),
    ("output_schema", []), ("parameters", [["summary"]]),
])
def test_snapshot_spec_decoder_rejects_malformed_contract_fields(field, value):
    snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control"}))))
    payload = snapshot_description(snapshot)
    payload["specs"][0][field] = value
    with pytest.raises(ValueError):
        restore_snapshot(payload)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_frozen_context_rejects_nonfinite_nested_spec_values(value):
    snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control"}))))
    payload = ProviderRequestContext(1, snapshot, None, "", ()).to_payload()
    payload["snapshot"]["specs"][0]["output_schema"]["extension"] = value
    with pytest.raises(ValueError, match="JSON"):
        ProviderRequestContext.from_payload(payload)


@pytest.mark.parametrize("value", [[], {}, 1, None, "invented"])
def test_kernel_result_rejects_invalid_delivery_discriminator(value):
    payload = KernelResult(False, "stopped", 1, "stopped").to_payload()
    with pytest.raises(ValueError):
        result_from_payload({**payload, "delivery": value})


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_text_tool_codec_rejects_nonfinite_arguments(value):
    decoded = decode_text_turn('{"tool":"read_file","args":{"path":"a","limit":' + value + '}}')
    assert isinstance(decoded, TextError)
    assert decoded.kind == "invalid_json"
