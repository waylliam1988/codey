"""Agreed partner transport identity, scoped to the Zen generation endpoint."""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

ZEN_BASE_URL = "https://opencode.ai/zen/v1"
CONNECTION_REVISION = "zen-public-partner-2026-10-07"
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_LOCK = threading.Lock()
_COUNTER = 0
_LAST_TIMESTAMP = 0


def identifier(prefix: str, *, descending: bool = False) -> str:
    global _COUNTER, _LAST_TIMESTAMP
    with _LOCK:
        timestamp = int(time.time() * 1000)
        if timestamp != _LAST_TIMESTAMP:
            _LAST_TIMESTAMP, _COUNTER = timestamp, 0
        _COUNTER += 1
        value = timestamp * 4096 + _COUNTER
    if descending:
        value = ~value
    stamp = (value & ((1 << 48) - 1)).to_bytes(6, "big").hex()
    suffix = "".join(_ALPHABET[byte % 62] for byte in secrets.token_bytes(14))
    return prefix + "_" + stamp + suffix


@dataclass(frozen=True)
class ZenIdentity:
    # Current partner agreement. These values never enter assistant prompts.
    user_agent: str = "opencode/1.18.35"
    client: str = "cli"
    session_id: str = field(default_factory=lambda: identifier("ses", descending=True))

    def headers(self, base_url: str) -> dict[str, str]:
        if base_url.rstrip("/") != ZEN_BASE_URL:
            raise ValueError("Zen identity destination is not allowed")
        return {"User-Agent": self.user_agent, "x-opencode-client": self.client,
                "x-opencode-session": self.session_id, "x-opencode-session-id": self.session_id,
                "x-opencode-request": identifier("msg"), "x-opencode-project": "global"}
