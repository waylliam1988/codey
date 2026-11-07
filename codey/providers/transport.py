"""Finite transport port. Python calling conventions belong to compatibility."""

from __future__ import annotations

from abc import ABC, abstractmethod

from codey.providers.requests import ProviderRequest


class ProviderTransport(ABC):
    provider: object

    @abstractmethod
    def execute_request(self, request: ProviderRequest) -> object:
        """Return an external reply; the reply codec validates it before core use."""
