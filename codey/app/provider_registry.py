"""Provider session, health, and ordering state for the app runtime."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from codey.providers.catalog import API_CONNECTIONS, PROVIDER_LABELS
from codey.providers.model_preferences import ModelPreferences
from codey.providers.supervisor import ProviderSupervisor

logger = logging.getLogger(__name__)


class ProviderRegistry:
    def __init__(self, state_home: str | Path | None = None) -> None:
        self.model_preferences = ModelPreferences(state_home)
        self.model_uses: Counter[tuple[str, str]] = Counter()
        self._sessions: dict[str, str] = {}
        self.supervisor = (
            ProviderSupervisor(state_home) if state_home else ProviderSupervisor()
        )

    def sessions_snapshot(self) -> dict[str, str]:
        return dict(self._sessions)

    def failover_order(
        self,
        tab_availability: Callable[[], dict[str, bool]],
    ) -> tuple[str, ...]:
        allowed = tuple(pid for pid in PROVIDER_LABELS if pid not in API_CONNECTIONS and self.model_preferences.allows(pid))
        if not allowed:
            return ()
        try:
            statuses = tab_availability()
        except Exception:
            logger.exception("provider tab availability probe failed")
            statuses = dict[str, bool]()
        opened = tuple(
            provider_id
            for provider_id in allowed
            if provider_id not in API_CONNECTIONS and statuses.get(provider_id)
        )
        return opened + tuple(
            provider_id
            for provider_id in allowed
            if provider_id not in API_CONNECTIONS and provider_id not in opened
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
            if provider_id != broken and self.model_preferences.allows(provider_id) and self.supervisor.is_available(provider_id)
        )

    def session_changed(self, provider_id: str, session_id: str) -> bool:
        return self._sessions.get(provider_id) != session_id

    def set_session(self, provider_id: str, session_id: str | None) -> None:
        if session_id:
            self._sessions[provider_id] = session_id
        else:
            self._sessions.pop(provider_id, None)

    def forget_session(self, session_id: str) -> None:
        for provider_id, owner in list(self._sessions.items()):
            if owner == session_id:
                self._sessions.pop(provider_id)


__all__ = ["ProviderRegistry"]
