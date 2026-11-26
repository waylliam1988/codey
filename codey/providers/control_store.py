"""Persistence of learned provider controls."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codey.providers import revival as provider_revival
from codey.providers.control_fingerprint import _page_host
from codey.storage.local_store import (
    DEFAULT_STATE_HOME,
    StoreCorruption,
    backup_corrupt_file,
    read_json_strict,
    write_json_atomic,
)

CONTROL_STORE = DEFAULT_STATE_HOME / "provider-controls.json"


def load_controls(path: Path | None = None) -> dict[str, Any]:
    path = path or CONTROL_STORE
    try:
        return read_json_strict(
            path,
            max_bytes=provider_revival.MAX_PROVIDER_STORE_BYTES,
        ) or {}
    except StoreCorruption:
        backup_corrupt_file(path)
        return {}


def save_control(
    provider_id: str,
    action: str,
    page: Any,
    fingerprint: dict[str, Any],
    *,
    path: Path | None = None,
    verified: bool = False,
) -> None:
    path = path or CONTROL_STORE
    data = load_controls(path)
    provider = data.setdefault(provider_id, {})
    if not isinstance(provider, dict):
        provider = dict[str, object]()
        data[provider_id] = provider
    provider[action] = {
        "host": _page_host(page),
        "fingerprint": fingerprint,
        "verified": verified,
        "failures": 0,
    }
    try:
        write_json_atomic(
            path,
            data,
            max_bytes=provider_revival.MAX_PROVIDER_STORE_BYTES,
        )
    except (OSError, ValueError) as exc:
        raise OSError(f"Could not save the learned control: {path}") from exc


def load_control(provider_id: str, action: str, *, path: Path | None = None) -> dict[str, Any] | None:
    data = load_controls(path)
    provider = data.get(provider_id)
    if not isinstance(provider, dict):
        return None
    record = provider.get(action)
    return record if isinstance(record, dict) else None
