"""Agreed partner transport identity, scoped to the Zen generation endpoint."""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

ZEN_BASE_URL = "https://opencode.ai/inference/openai/v1"
CONNECTION_REVISION = "zen-public-partner-openai-v1"
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_LOCK = threading.Lock()
_COUNTER = 0
_LAST_TIMESTAMP = 0


def validate_endpoint(value: object) -> str:
    if not isinstance(value, str) or len(value) > 512 or any(char.isspace() for char in value):
        raise ValueError("Zen API destination is invalid")
    url = urlsplit(value)
    if (url.scheme != "https" or url.hostname not in {"opencode.ai", "api.opencode.ai"}
            or url.username or url.password or url.port not in {None, 443} or url.query or url.fragment
            or not re.fullmatch(r"/(?:inference|zen)/[a-zA-Z0-9_/-]*v[0-9]+/?", url.path)
            or ".." in url.path or "//" in url.path):
        raise ValueError("Zen API destination requires an approved HTTPS host and API path")
    return urlunsplit(("https", url.hostname, url.path.rstrip("/"), "", ""))


def revision_for_endpoint(endpoint: str) -> str:
    return CONNECTION_REVISION + ":" + hashlib.sha256(validate_endpoint(endpoint).encode()).hexdigest()[:24]


def validate_generation_url(value: str) -> str:
    for suffix in ("/responses", "/chat/completions"):
        if value.endswith(suffix):
            return validate_endpoint(value[:-len(suffix)])
    raise ValueError("Zen fetch destination is not a generation endpoint")


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
    user_agent: str = "opencode/1.19.3"
    client: str = "cli"
    session_id: str = field(default_factory=lambda: identifier("ses", descending=True))

    def headers(self, base_url: str) -> dict[str, str]:
        validate_endpoint(base_url)
        return {"User-Agent": self.user_agent, "x-opencode-client": self.client,
                "x-opencode-session": self.session_id, "x-opencode-session-id": self.session_id,
                "x-opencode-request": identifier("msg"), "x-opencode-project": "global"}
