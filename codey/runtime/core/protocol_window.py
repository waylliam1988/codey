"""Opaque identities for a provider protocol window.

The values are intentionally independent of network clients and connector
modules so durable task records remain readable after an optional connector
is removed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4


class WindowMismatch(ValueError):
    """A result or recovery request belongs to another protocol window."""


def history_contract_id(value: object) -> str:
    """Return a stable digest for non-secret history interpretation settings."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProviderBinding:
    connection_id: str
    connection_revision: str
    model_id: str
    protocol: str
    history_contract_id: str

    def __post_init__(self) -> None:
        for name in ("connection_id", "connection_revision", "model_id", "protocol", "history_contract_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")

    def to_payload(self) -> dict[str, str]:
        return {
            "connection_id": self.connection_id,
            "connection_revision": self.connection_revision,
            "model_id": self.model_id,
            "protocol": self.protocol,
            "history_contract_id": self.history_contract_id,
        }

    @classmethod
    def from_payload(cls, payload: object) -> ProviderBinding:
        if not isinstance(payload, dict):
            raise ValueError("provider binding must be an object")
        keys = ("connection_id", "connection_revision", "model_id", "protocol", "history_contract_id")
        values = tuple(payload.get(key) for key in keys)
        if not all(isinstance(value, str) for value in values):
            raise ValueError("provider binding fields must be strings")
        connection_id, connection_revision, model_id, protocol, contract_id = values
        assert isinstance(connection_id, str)
        assert isinstance(connection_revision, str)
        assert isinstance(model_id, str)
        assert isinstance(protocol, str)
        assert isinstance(contract_id, str)
        return cls(connection_id, connection_revision, model_id, protocol, contract_id)


@dataclass(frozen=True)
class ProtocolWindowRef:
    session_id: str
    binding: ProviderBinding
    window_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(self.window_id, str) or not self.window_id.strip():
            raise ValueError("window_id must be a non-empty string")

    @classmethod
    def new(cls, session_id: str, binding: ProviderBinding) -> ProtocolWindowRef:
        return cls(session_id, binding, "window_" + uuid4().hex)

    def to_payload(self) -> dict[str, object]:
        return {"session_id": self.session_id, "binding": self.binding.to_payload(), "window_id": self.window_id}

    @classmethod
    def from_payload(cls, payload: object) -> ProtocolWindowRef:
        if not isinstance(payload, dict):
            raise ValueError("protocol window reference must be an object")
        session_id = payload.get("session_id")
        window_id = payload.get("window_id")
        if not isinstance(session_id, str) or not isinstance(window_id, str):
            raise ValueError("protocol window identity fields must be strings")
        return cls(
            session_id,
            ProviderBinding.from_payload(payload.get("binding")),
            window_id,
        )

    def require_compatible(self, other: ProtocolWindowRef) -> None:
        if self != other:
            raise WindowMismatch("protocol window reference does not match")


__all__ = ["ProviderBinding", "ProtocolWindowRef", "WindowMismatch", "history_contract_id"]
