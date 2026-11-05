"""Free eligibility is independent of model names, pricing and capability data."""
from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from codey.providers.cline.capabilities import ALIASES, Capability, parse_capability
from codey.providers.cline.http import JsonClient
from codey.providers.cline.identity import CAPABILITIES_URL, RECOMMENDED_URL
from codey.storage.local_store import read_json_strict, write_json_atomic

FREE_TTL = 300
CAPABILITY_TTL = 3600
SCHEMA_VERSION = 1


class Catalog:
    def __init__(self, home: Path, http: JsonClient, *, clock: Callable[[], float] = time.time) -> None:
        self.path = home / "catalog.json"
        self.http, self.clock = http, clock
        self.lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._generation = 0
        self.error = ""
        self.data: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "checked_at": 0,
                                    "capabilities_checked_at": 0, "free": [], "capabilities": {}}
        try:
            cached = read_json_strict(self.path)
            if cached and cached.get("schema_version") == SCHEMA_VERSION:
                self._validate(cached)
                self.data = cached
        except (OSError, ValueError, TypeError):
            self.error = "Model cache is invalid; refresh models."

    @staticmethod
    def _entries(raw: object) -> list[dict[str, str]]:
        if not isinstance(raw, list) or len(raw) > 2000:
            raise ValueError("invalid free catalog")
        entries: dict[str, dict[str, str]] = {}
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not 0 < len(item["id"]) <= 500:
                raise ValueError("invalid free catalog entry")
            name = item.get("name", item["id"])
            if not isinstance(name, str) or len(name) > 500:
                raise ValueError("invalid free catalog name")
            entries[item["id"]] = {"id": item["id"], "name": name}
        return list(entries.values())

    def _validate(self, cached: dict[str, Any]) -> None:
        self._entries(cached.get("free"))
        for key in ("checked_at", "capabilities_checked_at"):
            if type(cached.get(key)) not in (int, float) or cached[key] < 0:
                raise ValueError("invalid cache timestamp")
        if not isinstance(cached.get("capabilities"), dict):
            raise ValueError("invalid capability cache")

    def refresh(self, *, force: bool = False, deadline: float | None = None,
                cancelled: Callable[[], bool] = lambda: False) -> None:
        deadline = deadline if deadline is not None else time.monotonic() + 30
        while not self._refresh_lock.acquire(timeout=0.05):
            if cancelled() or time.monotonic() >= deadline:
                raise TimeoutError("catalog refresh cancelled or timed out")
        try:
            with self.lock:
                current = copy.deepcopy(self.data)
                generation = self._generation
            if not force and 0 <= self.clock() - current["checked_at"] < FREE_TTL:
                return
            free_reply = self.http.request(RECOMMENDED_URL, deadline=deadline, cancelled=cancelled)
            if free_reply.status != 200:
                raise ValueError("free directory unavailable")
            free = self._entries(free_reply.body.get("free"))
            capabilities = current["capabilities"]
            capability_time = current["capabilities_checked_at"]
            missing = any(entry["id"] not in capabilities and ALIASES.get(entry["id"]) not in capabilities for entry in free)
            if force or not capabilities or missing or self.clock() - capability_time >= CAPABILITY_TTL:
                reply = self.http.request(CAPABILITIES_URL, deadline=deadline, cancelled=cancelled)
                if reply.status != 200 or not isinstance(reply.body.get("data"), list):
                    raise ValueError("capability directory unavailable")
                wanted = {entry["id"] for entry in free} | {ALIASES.get(entry["id"], entry["id"]) for entry in free}
                capabilities = {item["id"]: item for item in reply.body["data"]
                                if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] in wanted}
                capability_time = self.clock()
            snapshot = {"schema_version": SCHEMA_VERSION, "checked_at": self.clock(), "free": free,
                        "capabilities_checked_at": capability_time, "capabilities": capabilities}
            if cancelled() or time.monotonic() >= deadline:
                raise TimeoutError("late catalog response discarded")
            with self.lock:
                if generation != self._generation:
                    return
                write_json_atomic(self.path, snapshot)
                self.data, self.error = snapshot, ""
        except (OSError, ValueError, RuntimeError, TimeoutError):
            with self.lock:
                self.error = "Could not refresh Cline free eligibility and capabilities; retry."
        finally:
            self._refresh_lock.release()

    def capability(self, model: str) -> Capability | None:
        with self.lock:
            values = self.data["capabilities"]
            value = values.get(model) or values.get(ALIASES.get(model, ""))
            return parse_capability(value) if isinstance(value, dict) else None

    def require(self, model: str, *, deadline: float | None = None,
                cancelled: Callable[[], bool] = lambda: False) -> Capability:
        self.refresh(deadline=deadline, cancelled=cancelled)
        with self.lock:
            if not 0 <= self.clock() - self.data["checked_at"] < FREE_TTL or self.error:
                raise ValueError("Cline free eligibility could not be refreshed; retry.")
            if not any(entry["id"] == model for entry in self.data["free"]):
                raise ValueError("This model is no longer in Cline's free directory; select another model.")
            capability = self.capability(model)
            if capability is None:
                raise ValueError("This free model has no verified capability contract.")
            return capability

    def payload(self) -> dict[str, Any]:
        with self.lock:
            models: list[dict[str, Any]] = []
            unavailable: list[dict[str, Any]] = []
            for entry in self.data["free"]:
                capability = self.capability(entry["id"])
                if capability is None:
                    unavailable.append({**entry, "availability": "capability_unknown",
                                        "unavailable_reason": "No verified model limits", "trial": True})
                    continue
                models.append({**entry, "protocol": "openai-completions", "efforts": list(capability.efforts),
                               "thinking_toggle": capability.toggle, "thinking_default_allowed": True,
                               "availability": "available", "trial": True})
            return {"id": "cline", "label": "Cline", "models": models, "unavailable_models": unavailable,
                    "checked_at": self.data["checked_at"], "stale": bool(self.error) or
                    not 0 <= self.clock() - self.data["checked_at"] < FREE_TTL, "error": self.error}

    def clear(self) -> None:
        # Never wait for a network refresh while holding the app admission gate.
        # Its late snapshot is rejected by generation, including after clear.
        with self.lock:
            self._generation += 1
            self.path.unlink(missing_ok=True)
            self.data = {"schema_version": SCHEMA_VERSION, "checked_at": 0, "capabilities_checked_at": 0,
                         "free": [], "capabilities": {}}
            self.error = ""
