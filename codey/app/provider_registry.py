"""Provider session, health, and ordering state for the app runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from codey.providers import PROVIDER_LABELS
from codey.providers.supervisor import ProviderSupervisor


class ProviderRegistry:
    def __init__(self, state_home: str | Path | None = None) -> None:
        self.sessions: dict[str, str] = {}
        self.supervisor = (
            ProviderSupervisor(state_home) if state_home else ProviderSupervisor()
        )
        self.ghost_learning_provider_factory = None
        self.ghost_router_provider_factory = None

    def failover_order(
        self,
        tab_availability: Callable[[], dict[str, bool]],
    ) -> tuple[str, ...]:
        try:
            statuses = tab_availability()
        except Exception:
            statuses = {}
        opened = tuple(
            provider_id
            for provider_id in PROVIDER_LABELS
            if provider_id != "local" and statuses.get(provider_id)
        )
        return opened + tuple(
            provider_id
            for provider_id in PROVIDER_LABELS
            if provider_id != "local" and provider_id not in opened
        )

    def self_repair_candidates(
        self,
        broken_provider_id: str,
        *,
        ordered: tuple[str, ...],
    ) -> tuple[str, ...]:
        broken = str(broken_provider_id or "").strip().lower()
        return tuple(
            provider_id
            for provider_id in ordered
            if provider_id != broken and self.supervisor.is_available(provider_id)
        )

    def session_changed(self, provider_id: str, session_id: str) -> bool:
        return self.sessions.get(provider_id) != session_id

    def set_session(self, provider_id: str, session_id: str | None) -> None:
        if session_id:
            self.sessions[provider_id] = session_id
        else:
            self.sessions.pop(provider_id, None)

    def forget_session(self, session_id: str) -> None:
        for provider_id, owner in list(self.sessions.items()):
            if owner == session_id:
                self.sessions.pop(provider_id)


__all__ = ["ProviderRegistry"]

