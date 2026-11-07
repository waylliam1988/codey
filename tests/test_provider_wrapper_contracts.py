"""Typed wrapper interception preserves transport, capability and receipt semantics."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from codey.operations.kernel_actions import _bind_attempt_gate
from codey.operations.kernel_capabilities import KernelCapabilityView
from codey.operations.kernel_contracts import KernelTransportDeps
from codey.operations.provider_session import (
    ConversationProvider,
    DeadlineProvider,
    ObservedProvider,
    ProviderAdapter,
    bind_api_usage,
    normalize_provider_reply,
)
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.providers.compat_transport import execute_compat_request, provider_attribute
from codey.providers.requests import ProviderRequestOptions, SendTextRequest
from codey.providers.token_accounting import ApiExchangeUsage, ContextBudget, ReportedUsage, RequestContextCount
from codey.runtime.core.protocol_window import ProtocolWindowRef, ProviderBinding


def test_wrappers_have_no_open_attribute_surface():
    wrapper = ProviderAdapter(SimpleNamespace(secret="extension"))
    with pytest.raises(AttributeError):
        _ = wrapper.secret
    assert "__getattr__" not in ProviderAdapter.__dict__


def test_nested_wrappers_observe_typed_request_once_and_normalize_once():
    raw = Mock(spec=["send", "normalize_reply"])
    raw.send.return_value = "reply"
    # Explicit instance declaration, not Mock's dynamic attribute fabrication.
    raw.normalize_reply = Mock(return_value="normalized")
    seen = []
    recorder = Mock()
    wrapped = ObservedProvider(ConversationProvider(raw, recorder), seen.append)
    request = SendTextRequest("prompt", ProviderRequestOptions(supplied=frozenset({"timeout"})))
    reply = execute_compat_request(wrapped, request)
    assert normalize_provider_reply(wrapped, reply) == "normalized"
    assert seen == [request]
    raw.send.assert_called_once_with("prompt", timeout=None)
    raw.normalize_reply.assert_called_once_with("reply")
    recorder.record_exchange.assert_called_once_with("prompt", "reply")


def test_native_reply_detaches_arguments_and_rejects_malformed_fields():
    args = {"names": ["first"]}
    raw = AssistantTurn(tool_calls=(ProviderToolCall("call", "tool", args),))
    normalized = normalize_provider_reply(object(), raw)
    args["names"].append("later")
    assert normalized.tool_calls[0].arguments == {"names": ["first"]}
    for bad in (replace(raw, finish="fake"), replace(raw, text=1), replace(raw, tool_calls=(object(),)),
                AssistantTurn(tool_calls=(ProviderToolCall("id", "tool", {"bad": object()}),))):
        with pytest.raises(ValueError):
            normalize_provider_reply(object(), bad)


def test_declared_normalizer_can_reject_and_never_accepts_invalid_hook_result():
    provider = SimpleNamespace(normalize_reply=lambda _: object())
    with pytest.raises(ValueError, match="unsupported response"):
        normalize_provider_reply(ObservedProvider(provider, lambda _: None), "raw")


def test_window_capability_and_attempt_gate_survive_wrapping():
    window = ProtocolWindowRef("session", ProviderBinding("id", "rev", "model", "text", "contract"), "window")
    bound = []
    raw = SimpleNamespace(send=lambda _: "reply", window_ref=window, text_tool_format="json_schema",
                          request_scoped_tool_context=True, bind_attempt_gate=bound.append)
    wrapped = ObservedProvider(ConversationProvider(raw, Mock()), lambda _: None)
    view = KernelCapabilityView.from_provider(wrapped, provider_id="local")
    assert view.window_ref == window
    assert view.text_tool_format == "json_schema"
    assert view.request_scoped_tool_context
    def gate(client, payload):
        pass
    deps = KernelTransportDeps(provider=wrapped, attempt_gate=gate)
    assert _bind_attempt_gate(wrapped, deps, None)
    assert bound == [gate]
    assert not _bind_attempt_gate(ProviderAdapter(object()), deps, None)


def test_wrapper_episode_timeout_is_not_lost_during_capability_lookup():
    with patch("time.monotonic", return_value=0):
        wrapped = ObservedProvider(DeadlineProvider(SimpleNamespace(timeout=300), timeout=5), lambda _: None)
    assert provider_attribute(wrapped, "timeout") == 5


def test_positional_timeout_is_limited_without_duplicate_keyword():
    raw = Mock(spec=["send"])
    with patch("time.monotonic", return_value=100):
        bounded = DeadlineProvider(raw, timeout=10)
    with patch("time.monotonic", return_value=105):
        bounded.send("prompt", 20)
    raw.send.assert_called_once_with("prompt", timeout=5)


def test_usage_binding_passes_validated_usage_through_nested_wrappers():
    budget = ContextBudget(1000, 100, 10, 100)
    callbacks = []
    raw = SimpleNamespace(context_budget=budget, bind_usage=lambda cid, sink: callbacks.append((cid, sink)))
    trace = Mock()
    bind_api_usage(ObservedProvider(ProviderAdapter(raw), lambda _: None), "connection", trace)
    record = ApiExchangeUsage("exchange", "connection", "model", "text", RequestContextCount(1, "estimated"),
                              budget, ReportedUsage(), "unknown", "ok")
    assert callbacks[0][0] == "connection"
    callbacks[0][1](record)
    trace.record_api_usage.assert_called_once_with(record)
