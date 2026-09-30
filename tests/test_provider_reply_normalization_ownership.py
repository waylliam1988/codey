"""Provider reply normalization is owned by provider wrappers.

``provider_session`` owns ``normalize_provider_reply`` (and its explicit-hook
helper). ``kernel_transport`` consumes it for sending but never defines it,
so the wrapper -> transport direction is one-way. Behavior: only explicitly
declared hooks run, multi-layer wrappers still resolve the inner capability,
cycles fail closed, and dynamic attribute faking cannot forge detection.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_normalizer_lives_in_provider_session() -> None:
    from codey.operations import provider_session

    assert callable(getattr(provider_session, "normalize_provider_reply", None))
    assert callable(getattr(provider_session, "_explicit_reply_normalizer", None))


def test_kernel_transport_delegates_without_defining() -> None:
    text = (ROOT / "codey/operations/kernel_transport.py").read_text(encoding="utf-8-sig")
    assert "def normalize_provider_reply" not in text
    assert "def _explicit_reply_normalizer" not in text
    assert "provider_session" in text


def test_normalization_calls_declared_hook_once() -> None:
    from codey.operations.provider_session import ProviderAdapter, normalize_provider_reply

    calls: list[object] = []

    class Hooked:
        def normalize_reply(self, reply: object) -> object:
            calls.append(reply)
            return f"hooked:{reply}"

    assert normalize_provider_reply(Hooked(), "r") == "hooked:r"
    assert calls == ["r"]
    # ProviderAdapter delegates to the same owner without re-wrapping.
    assert ProviderAdapter(Hooked()).normalize_reply("r") == "hooked:r"
    assert calls == ["r", "r"]


def test_undeclared_hook_returns_reply_untouched() -> None:
    from codey.operations.provider_session import normalize_provider_reply

    class Plain:
        pass

    sentinel = object()
    assert normalize_provider_reply(Plain(), sentinel) is sentinel


def test_dynamic_getattr_cannot_forge_normalizer() -> None:
    from codey.operations.provider_session import _explicit_reply_normalizer

    class Sneaky:
        def __getattr__(self, name: str) -> object:
            if name == "normalize_reply":
                return lambda reply: "forged"
            raise AttributeError(name)

    assert _explicit_reply_normalizer(Sneaky()) is None


def test_cyclic_adapter_chain_fails_closed() -> None:
    from codey.operations.kernel_transport import provider_uses_native
    from codey.operations.provider_session import ProviderAdapter

    first = ProviderAdapter(object())
    second = ProviderAdapter(first)
    # Create a real cycle: first wraps second, second wraps first.
    first.provider = second  # type: ignore[attr-defined]
    try:
        provider_uses_native(first, provider_id="x")
    except ValueError as exc:
        assert "cyclic" in str(exc).lower()
    else:
        raise AssertionError("cyclic wrapper chain must raise ValueError")
