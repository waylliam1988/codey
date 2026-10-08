"""Durable model allowlists. Discovery never changes a user's selection."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from codey.providers.catalog import API_CONNECTIONS, WEB_PROVIDER_LABELS
from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import DEFAULT_STATE_HOME, read_json_strict, write_json_atomic


class ModelPreferencesConflict(ValueError):
    pass


class ModelDisabledError(ValueError):
    pass


def default_sources() -> dict[str, Any]:
    return {
        "websites": {"enabled": True, "models": list(WEB_PROVIDER_LABELS)},
        **{key: {"enabled": False, "models": []} for key in API_CONNECTIONS},
    }


def normalize_sources(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(default_sources()):
        raise ValueError("model sources changed; reload Settings")
    clean: dict[str, Any] = {}
    for key, source in value.items():
        if not isinstance(source, dict) or set(source) != {"enabled", "models"} or type(source["enabled"]) is not bool:
            raise ValueError("source enabled must be true or false")
        models = source["models"]
        if (
            not isinstance(models, list)
            or len(models) > 2000
            or any(not isinstance(x, str) or not x or len(x) > 500 for x in models)
        ):
            raise ValueError("models must be a list of model IDs")
        if key == "websites" and any(x not in WEB_PROVIDER_LABELS for x in models):
            raise ValueError("unknown website model")
        clean[key] = {"enabled": source["enabled"] and bool(models), "models": list(dict.fromkeys(models))}
    return clean


def allows(sources: dict[str, Any], provider_id: str, model_id: str = "") -> bool:
    source_id = "websites" if provider_id in WEB_PROVIDER_LABELS else provider_id
    source = sources.get(source_id)
    if not source or source.get("enabled") is not True:
        return False
    selected = source["models"]
    return (
        provider_id in selected if source_id == "websites" else (model_id in selected if model_id else bool(selected))
    )


class ModelPreferences:
    def __init__(self, state_home: str | Path | None = None) -> None:
        self.path = Path(state_home or DEFAULT_STATE_HOME) / "model-preferences.json"

    def _read(self) -> dict[str, Any]:
        raw = read_json_strict(self.path)
        if raw is None:
            return {"revision": 0, "sources": default_sources(), "catalogs": {}}
        if (
            type(raw.get("revision")) is not int
            or not isinstance(raw.get("sources"), dict)
            or not isinstance(raw.get("catalogs"), dict)
        ):
            raise ValueError("invalid model preferences")
        # Removed integrations cease to participate; their old chats own their identity.
        current = {key: raw["sources"].get(key, value) for key, value in default_sources().items()}
        return {"revision": raw["revision"], "sources": normalize_sources(current), "catalogs": raw["catalogs"]}

    def snapshot(self) -> dict[str, Any]:
        value = self._read()
        return copy.deepcopy({"revision": value["revision"], "sources": value["sources"]})

    def save(self, sources: object, *, base_revision: int) -> dict[str, Any]:
        clean = normalize_sources(sources)
        with with_file_lock(self.path):
            value = self._read()
            if type(base_revision) is not int or base_revision != value["revision"]:
                raise ModelPreferencesConflict("Model settings changed; reload Settings.")
            value.update(sources=clean, revision=base_revision + 1)
            write_json_atomic(self.path, value)
        return self.snapshot()

    def allows(self, provider_id: str, model_id: str = "") -> bool:
        return allows(self._read()["sources"], provider_id, model_id)

    def enabled(self, source_id: str) -> bool:
        return self._read()["sources"].get(source_id, {}).get("enabled") is True

    def catalog(self, source_id: str) -> dict[str, Any]:
        return copy.deepcopy(self._read()["catalogs"].get(source_id, {"id": source_id, "models": []}))

    def observe_catalog(self, payload: dict[str, Any]) -> None:
        source_id = payload.get("id")
        if source_id not in API_CONNECTIONS:
            raise ValueError("model source unavailable")
        # Only display/capability facts are stored. Never persist credentials or headers.
        clean = {
            key: copy.deepcopy(payload[key])
            for key in ("id", "label", "models", "base_url", "default_model", "connected", "error", "stale")
            if key in payload
        }
        clean["models"] = [
            {
                key: copy.deepcopy(model[key])
                for key in ("id", "name", "efforts", "protocol", "review_eligible")
                if key in model
            }
            for model in payload.get("models", [])
        ]
        with with_file_lock(self.path):
            value = self._read()
            value["catalogs"][source_id] = clean
            write_json_atomic(self.path, value)
