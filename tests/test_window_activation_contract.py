"""Window planning is declarative and activation is the only mutating step."""

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
