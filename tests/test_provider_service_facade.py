"""The app facade delegates browser defaults and explicit options to the registry."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from codey.app import provider_services
from codey.providers.base import ChatProvider


@dataclass
class Provider:
    name: str = "fixture"
    location: str = "fixture://provider"

    def new_chat(self, timeout: float | None = None) -> None:
        pass

    def send(self, text: str, timeout: float | None = None) -> str:
        return text

    def close(self) -> None:
        pass


def test_browser_defaults_and_explicit_options_are_owned_by_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.providers import registry

    calls: list[tuple[str, int, Path, bool, bool]] = []
    default_profile = tmp_path / "registry-default"
    explicit_profile = tmp_path / "explicit"
    provider = Provider()
    statuses = {"deepseek": True}

    def warm(*, port: int = 43210, profile: Path = default_profile) -> dict[str, bool]:
        calls.append(("warm", port, profile, False, False))
        return statuses

    def connect(
        provider_id: str, *, port: int = 43210, profile: Path = default_profile,
        open_if_missing: bool = True, bring_to_front: bool = True,
    ) -> ChatProvider:
        calls.append((provider_id, port, profile, open_if_missing, bring_to_front))
        return provider

    def fresh(provider_id: str, *, port: int = 43210, profile: Path = default_profile) -> ChatProvider:
        calls.append((provider_id, port, profile, False, False))
        return provider

    monkeypatch.setattr(registry, "warm_provider_tabs", warm)
    monkeypatch.setattr(registry, "connect_provider", connect)
    monkeypatch.setattr(registry, "connect_fresh_provider_tab", fresh)
    assert provider_services.warm_provider_tabs() is statuses
    assert provider_services.connect_provider("deepseek") is provider
    assert provider_services.connect_fresh_provider_tab("deepseek") is provider
    assert calls == [
        ("warm", 43210, default_profile, False, False),
        ("deepseek", 43210, default_profile, True, True),
        ("deepseek", 43210, default_profile, False, False),
    ]
    calls.clear()
    assert provider_services.warm_provider_tabs(port=54321, profile=explicit_profile) is statuses
    assert provider_services.connect_provider(
        provider_id="deepseek", port=54321, profile=explicit_profile,
        open_if_missing=False, bring_to_front=False,
    ) is provider
    assert provider_services.connect_fresh_provider_tab(
        "deepseek", port=54321, profile=explicit_profile,
    ) is provider
    assert calls == [
        ("warm", 54321, explicit_profile, False, False),
        ("deepseek", 54321, explicit_profile, False, False),
        ("deepseek", 54321, explicit_profile, False, False),
    ]


def test_registry_errors_propagate_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.providers import registry

    failure = RuntimeError("browser unavailable")

    def warm() -> dict[str, bool]:
        raise failure

    monkeypatch.setattr(registry, "warm_provider_tabs", warm)
    with pytest.raises(RuntimeError) as caught:
        provider_services.warm_provider_tabs()
    assert caught.value is failure
