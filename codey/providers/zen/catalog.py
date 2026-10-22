"""Official model directory and endpoint discovery, with bounded ETag cache."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from codey.providers.api_transport import NoGenerationRedirect
from codey.providers.zen.access import ZenAccessObservations
from codey.providers.zen.declarations import TRANSPORT_CONTRACT
from codey.providers.zen.identity import CONNECTION_REVISION, ZenIdentity, validate_endpoint
from codey.storage.local_store import StoreCorruption, read_json_strict, write_json_atomic

DIRECTORY_URL = "https://models.opencode.ai/api.json"
CACHE_TTL_S = 5 * 60
PROTOCOLS = {"@ai-sdk/openai": "openai-responses", "@ai-sdk/openai-compatible": "openai-completions"}


@dataclass(frozen=True)
class ZenModel:
    id: str
    name: str
    protocol: str
    context: int
    output: int
    api: str
    efforts: tuple[str, ...] = ()
    input_limit_tokens: int | None = None
    thinking_toggle: bool = False

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def _model_limits(model: dict[str, Any]) -> tuple[int, int, int | None] | None:
    limits = model.get("limit")
    if not isinstance(limits, dict):
        return None
    context, output = limits.get("context"), limits.get("output")
    if type(context) is not int or context < 1024 or type(output) is not int or output <= 0:
        return None
    input_limit = limits.get("input")
    if input_limit is not None and (type(input_limit) is not int or input_limit <= 0):
        return None
    return context, output, input_limit


def parse_models(directory: object) -> tuple[ZenModel, ...]:
    if not isinstance(directory, dict) or not isinstance(directory.get("opencode"), dict):
        raise ValueError("Zen directory is missing the provider")
    provider = directory["opencode"]
    models = provider.get("models")
    if not isinstance(models, dict):
        raise ValueError("Zen directory has malformed models")
    if not models:
        return ()
    provider_api = validate_endpoint(provider.get("api"))
    result: list[ZenModel] = []
    for identity, model in models.items():
        if not isinstance(identity, str) or not isinstance(model, dict):
            continue
        if model.get("status") == "deprecated":
            continue
        cost = model.get("cost") or {}
        if not isinstance(cost, dict) or any(type(cost.get(key)) not in {int, float} or cost[key] != 0 for key in ("input", "output")):
            continue
        if model.get("tool_call") is not True:
            continue
        implementation = model.get("provider") or {}
        if not isinstance(implementation, dict):
            continue
        try:
            api = validate_endpoint(implementation.get("api", provider_api))
        except ValueError:
            continue
        npm = implementation.get("npm", provider.get("npm"))
        protocol = PROTOCOLS.get(npm) if isinstance(npm, str) else None
        if protocol is None:
            continue
        limits = _model_limits(model)
        if limits is None:
            continue
        context, output, input_limit = limits
        options = model.get("reasoning_options") or []
        if not isinstance(options, list):
            continue
        thinking_toggle = any(isinstance(option, dict) and option.get("type") == "toggle" for option in options)
        efforts = tuple(value for option in options if isinstance(option, dict) and option.get("type") == "effort"
                        and isinstance(option.get("values"), list) for value in option["values"]
                        if isinstance(value, str) and value in {"minimal", "low", "medium", "high", "xhigh", "max"})
        result.append(ZenModel(identity, str(model.get("name") or identity), protocol, context, output, api, efforts,
                               input_limit, thinking_toggle))
    return tuple(sorted(result, key=lambda model: model.id))


def fetch_catalog(url: str, etag: str) -> tuple[dict[str, Any] | None, str]:
    headers = {"User-Agent": ZenIdentity().user_agent, "Accept": "application/json"}
    if etag:
        headers["If-None-Match"] = etag
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.build_opener(NoGenerationRedirect()).open(request, timeout=10) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("Zen directory exceeded its byte limit")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("Zen directory is not an object")
            return body, str(response.headers.get("ETag") or "")
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 304:
            return None, etag
        raise


class ZenCatalog:
    def __init__(self, state_home: Path, *, fetch: Callable[[str, str], tuple[dict[str, Any] | None, str]] = fetch_catalog) -> None:
        self.path = state_home / "connections" / "zen-catalog.json"
        self.access = ZenAccessObservations(state_home, CONNECTION_REVISION + ":" + TRANSPORT_CONTRACT)
        self.fetch = fetch
        self._lock = threading.RLock()
        self._directory: dict[str, Any] = {}
        self.models: tuple[ZenModel, ...] = ()
        self.etag = ""
        self.updated = 0.0
        self.checked = 0.0
        self.stale = False
        self.error = ""
        self._refresh_version = 0
        try:
            cache = read_json_strict(self.path)
            if isinstance(cache, dict) and cache.get("schema_version") == 2:
                directory = cache["directory"]
                self.models = parse_models(directory)
                self._directory = cache["directory"]
                self.etag = cache["etag"]
                self.updated = float(cache["updated"])
                self.checked = self.updated
        except (StoreCorruption, KeyError, ValueError, TypeError):
            self.stale = True

    def refresh(self, *, force: bool = False) -> tuple[ZenModel, ...]:
        with self._lock:
            if not force and time.time() - self.checked < CACHE_TTL_S:
                return self.models
            old_directory, old_etag = self._directory, self.etag
            self._refresh_version += 1
            version = self._refresh_version
        try:
            directory, etag = self.fetch(DIRECTORY_URL, old_etag)
            directory = directory if directory is not None else old_directory
            models = parse_models(directory)
            snapshot = {"opencode": directory["opencode"]}
            updated = time.time()
            with self._lock:
                if version != self._refresh_version:
                    return self.models
                write_json_atomic(self.path, {"schema_version": 2, "directory": snapshot,
                                          "etag": etag, "updated": updated})
                self._directory, self.models, self.etag, self.updated = snapshot, models, etag, updated
                self.checked = updated
                self.stale, self.error = False, ""
        except (OSError, ValueError, TypeError, KeyError) as exc:
            with self._lock:
                if version == self._refresh_version:
                    self.stale, self.error = True, str(exc)[:240]
                    self.checked = time.time()
        with self._lock:
            return self.models

    def cached_payload(self) -> dict[str, Any]:
        """Project raw cached facts locally, including names of retired models."""
        with self._lock:
            unavailable = [
                {"id": identity, "name": str(model.get("name") or identity), "status": "deprecated"}
                for identity, model in self._directory.get("opencode", {}).get("models", {}).items()
                if isinstance(model, dict) and model.get("status") == "deprecated"
            ]
            return {"models": [model.to_payload() for model in self.models], "unavailable_models": unavailable,
                    "stale": self.stale, "error": self.error, "updated_at": self.updated,
                    "checked_at": self.checked}

    def require_cached(self, model_id: str, *, protocol: str = "") -> ZenModel:
        """Validate a frozen selection without making a request."""
        with self._lock:
            models = self.models
        for model in models:
            if model.id == model_id and (not protocol or model.protocol == protocol):
                return model
        raise ValueError("selected model is not present in the cached Zen catalog")

    def require(self, model_id: str, *, protocol: str = "") -> ZenModel:
        for model in self.refresh():
            if model.id == model_id and (not protocol or model.protocol == protocol):
                return model
        model = self._directory.get("opencode", {}).get("models", {}).get(model_id)
        if isinstance(model, dict) and model.get("status") == "deprecated":
            raise ValueError("selected Zen model is deprecated and no longer available; choose another model")
        if isinstance(model, dict) and _model_limits(model) is None:
            raise ValueError("selected Zen model is missing valid context and output limits")
        raise ValueError("selected model is not currently listed as a supported free Zen model")
