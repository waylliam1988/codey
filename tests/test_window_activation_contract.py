"""Window planning is declarative and activation is the only mutating step."""

import pytest

from codey.durable.context import ContextSnapshot
from codey.operations.conversation_window import OpenWindow, WindowActivationError, WindowOpenReason, activate_window
from codey.runtime.core.protocol_window import ProtocolWindowRef, ProviderBinding


@pytest.mark.parametrize("method", ["new_chat", "reset_window"])
def test_missing_context_loader_rejects_before_opening_window(method: str) -> None:
    ref = ProtocolWindowRef("s", ProviderBinding("local", "r", "m", "chat", "c"), "w")
    resets: list[str] = []

    def open_window() -> ProtocolWindowRef:
        resets.append(method)
        return ref

    class Provider:
        pass

    provider = Provider()
    setattr(provider, method, open_window)
    context = ContextSnapshot(("entry",), ({"role": "user", "content": "Prior turn"},), 1, "digest", ())
    action = OpenWindow(WindowOpenReason.USER_RESET, context=context)

    with pytest.raises(WindowActivationError, match="cannot load planned durable context"):
        activate_window(provider, action)
    assert resets == []


def test_legacy_reset_passes_the_context_and_entry_ids_after_success() -> None:
    ref = ProtocolWindowRef("s", ProviderBinding("local", "r", "m", "chat", "c"), "w")
    calls: list[str] = []

    class Provider:
        def reset_window(self) -> ProtocolWindowRef:
            calls.append("reset")
            return ref

        def load_durable_context(self, messages: list[dict[str, object]], *,
                                 source_entry_ids: tuple[str, ...]) -> None:
            assert messages == [{"role": "user", "content": "Prior turn"}]
            assert source_entry_ids == ("entry",)
            calls.append("restore")

    context = ContextSnapshot(("entry",), ({"role": "user", "content": "Prior turn"},), 1, "digest", ())
    assert activate_window(Provider(), OpenWindow(WindowOpenReason.USER_RESET, context=context)) == ref
    assert calls == ["reset", "restore"]


def test_window_actions_have_explicit_reuse_or_open_semantics() -> None:
    from codey.operations.conversation_window import OpenWindow, ReuseWindow, WindowOpenReason
    from codey.runtime.core.protocol_window import ProtocolWindowRef, ProviderBinding

    ref = ProtocolWindowRef("s", ProviderBinding("local", "r", "m", "chat", "c"), "w")
    assert ReuseWindow(ref).ref == ref
    assert OpenWindow(WindowOpenReason.USER_RESET).reason is WindowOpenReason.USER_RESET


def test_browser_provider_can_activate_first_window_via_new_chat() -> None:
    from codey.operations.conversation_window import OpenWindow, WindowOpenReason, activate_window

    class BrowserProvider:
        def __init__(self) -> None:
            self.calls = 0

        def new_chat(self) -> None:
            self.calls += 1

    provider = BrowserProvider()
    assert activate_window(provider, OpenWindow(WindowOpenReason.FIRST_OPEN)) is None
    assert provider.calls == 1
