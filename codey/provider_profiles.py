"""Validated, data-only profiles for supported web chat surfaces."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


PROFILE_PATH = Path(__file__).with_name("provider_profiles.json")
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ProviderProfile:
    provider_id: str
    version: int
    hosts: tuple[str, ...]
    selectors_by_action: dict[str, tuple[str, ...]]

    def selectors(self, action: str) -> tuple[str, ...]:
        return self.selectors_by_action.get(action, ())

    def selector(self, action: str) -> str:
        values = self.selectors(action)
        if not values:
            raise KeyError(f"Provider {self.provider_id!r} has no {action!r} selector")
        return values[0]

    def combined(self, action: str) -> str:
        return ", ".join(self.selectors(action))


@lru_cache(maxsize=1)
def load_profiles(path: Path = PROFILE_PATH) -> dict[str, ProviderProfile]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not load provider profiles: {path}") from exc
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("Unsupported provider profile schema")
    profiles = raw.get("profiles")
    if not isinstance(profiles, dict):
        raise RuntimeError("Provider profiles must contain a profiles object")
    return {
        provider_id: _parse_profile(provider_id, payload)
        for provider_id, payload in profiles.items()
    }


def get_profile(provider_id: str) -> ProviderProfile:
    try:
        return load_profiles()[provider_id]
    except KeyError as exc:
        raise KeyError(f"Unknown provider profile: {provider_id}") from exc


def _parse_profile(provider_id: str, payload: Any) -> ProviderProfile:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid provider profile: {provider_id}")
    version = payload.get("version")
    hosts = payload.get("hosts")
    selectors = payload.get("selectors")
    if not isinstance(version, int) or version < 1:
        raise RuntimeError(f"Invalid profile version: {provider_id}")
    if not isinstance(hosts, list) or not hosts or not all(isinstance(item, str) and item for item in hosts):
        raise RuntimeError(f"Invalid profile hosts: {provider_id}")
    if not isinstance(selectors, dict):
        raise RuntimeError(f"Invalid profile selectors: {provider_id}")
    parsed: dict[str, tuple[str, ...]] = {}
    for action, values in selectors.items():
        if not isinstance(action, str) or not isinstance(values, list):
            raise RuntimeError(f"Invalid selector list: {provider_id}")
        clean = tuple(value for value in values if isinstance(value, str) and value.strip())
        if not clean:
            raise RuntimeError(f"Empty selector list: {provider_id}/{action}")
        parsed[action] = clean
    for required in ("message_box", "send_button", "response"):
        if required not in parsed:
            raise RuntimeError(f"Missing selector list: {provider_id}/{required}")
    return ProviderProfile(
        provider_id=provider_id,
        version=version,
        hosts=tuple(host.lower() for host in hosts),
        selectors_by_action=parsed,
    )
