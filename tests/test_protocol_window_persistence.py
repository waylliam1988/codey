"""Protocol window identity is distinct from chat and run identity."""

import pytest


def test_window_ref_round_trips_without_secrets() -> None:
    from codey.runtime.core.protocol_window import ProtocolWindowRef, ProviderBinding

    binding = ProviderBinding("local", "rev-1", "model", "openai-responses", "contract-1")
    ref = ProtocolWindowRef("session-1", binding, "window-1")
    payload = ref.to_payload()
    assert ProtocolWindowRef.from_payload(payload) == ref
    assert "api_key" not in repr(payload)


def test_binding_mismatch_is_explicit() -> None:
    from codey.runtime.core.protocol_window import ProtocolWindowRef, ProviderBinding, WindowMismatch

    left = ProtocolWindowRef("s", ProviderBinding("local", "r1", "m1", "chat", "c1"), "w1")
    right = ProtocolWindowRef("s", ProviderBinding("local", "r2", "m1", "chat", "c1"), "w1")
    with pytest.raises(WindowMismatch):
        left.require_compatible(right)


def test_protocol_ledger_keeps_window_id_across_reopen(tmp_path) -> None:
    from codey.providers.context_ledger import ContextLedger
    from codey.runtime.core.protocol_window import ProtocolWindowRef, ProviderBinding

    binding = ProviderBinding("local", "r1", "m", "openai-responses", "c1")
    ref = ProtocolWindowRef("session-1", binding, "window-1")
    ledger = ContextLedger.for_protocol_session(tmp_path, "session-1", ref)
    ledger.commit([], window_ref=ref)
    reopened = ContextLedger.for_protocol_session(tmp_path, "session-1", ref)
    assert reopened.window_ref == ref


def test_reset_rejects_open_native_call(tmp_path) -> None:
    from codey.providers.api_provider import ApiProvider

    provider = ApiProvider("http://fixture/v1", "model", api_protocol="openai-responses")
    provider.bind_context("session-1", tmp_path)
    provider._messages = [{"type": "function_call", "call_id": "call-1", "name": "shell", "arguments": "{}"}]
    with pytest.raises(ValueError, match="pending"):
        provider.reset_window()


def test_failed_reset_keeps_window_reference_and_view(tmp_path, monkeypatch) -> None:
    from codey.providers.api_provider import ApiProvider

    provider = ApiProvider("http://fixture/v1", "model", api_protocol="openai-responses")
    provider.bind_context("session-1", tmp_path)
    provider._messages = [{"role": "user", "content": "accepted"}]
    old_ref = provider.window_ref
    old_view = list(provider._messages)

    def fail(*args, **kwargs):
        raise OSError("head write failed")

    monkeypatch.setattr(provider.context_ledger, "commit", fail)
    with pytest.raises(OSError, match="head write"):
        provider.reset_window()
    assert provider.window_ref == old_ref
    assert provider._messages == old_view


def test_model_switch_rejects_persisted_pending_call_without_erasing_history(tmp_path) -> None:
    """A binding change must fail closed while the old window has an open call."""
    from codey.providers.api_provider import ApiProvider

    old = ApiProvider("http://fixture/v1", "model-a", api_protocol="openai-responses")
    old.bind_context("session-1", tmp_path)
    ref = old.window_ref
    assert ref is not None
    pending = [{"type": "function_call", "call_id": "call-1", "name": "shell", "arguments": "{}"}]
    old.context_ledger.commit(pending, window_ref=ref)

    switched = ApiProvider("http://fixture/v1", "model-b", api_protocol="openai-responses")
    with pytest.raises(ValueError, match="calls are pending"):
        switched.bind_context("session-1", tmp_path)

    # The failed switch leaves the durable protocol view and binding intact.
    reopened = type(old.context_ledger).for_protocol_session(tmp_path, "session-1", ref)
    assert reopened.window_ref == ref
    assert reopened.view == pending


def test_abort_does_not_erase_accepted_protocol_history(tmp_path) -> None:
    from codey.providers.api_provider import ApiProvider

    provider = ApiProvider("http://fixture/v1", "model", api_protocol="openai-responses")
    provider.bind_context("session-1", tmp_path)
    provider._messages = [{"role": "user", "content": "accepted"}]
    provider.abandon_inflight()
    assert provider._messages == [{"role": "user", "content": "accepted"}]


def test_bound_tool_delivery_rejects_wrong_window_before_transport(tmp_path, monkeypatch) -> None:
    from codey.providers.api_provider import ApiProvider
    from codey.providers.base import ProviderToolResult
    from codey.runtime.core.protocol_window import ProtocolWindowRef

    provider = ApiProvider("http://fixture/v1", "model", api_protocol="openai-responses")
    provider.bind_context("session-1", tmp_path)
    current = provider.window_ref
    assert current is not None
    wrong = ProtocolWindowRef.new("session-1", current.binding)
    monkeypatch.setattr("codey.providers.api_transport.generate", lambda *a, **k: (_ for _ in ()).throw(AssertionError("HTTP")))
    with pytest.raises(ValueError, match="window reference"):
        provider.send_tool_results_bound([ProviderToolResult("call-1", "ok")], wrong)
