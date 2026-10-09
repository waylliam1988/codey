"""Public directory plus actual endpoint listing, with bounded ETag cache."""
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
from codey.providers.zen.identity import CONNECTION_REVISION, ZEN_BASE_URL, ZenIdentity
from codey.storage.local_store import StoreCorruption, read_json_strict, write_json_atomic

DIRECTORY_URL = "https://models.opencode.ai/api.json"
PROTOCOLS = {"@ai-sdk/openai": "openai-responses", "@ai-sdk/openai-compatible": "openai-completions"}


@dataclass(frozen=True)
class ZenModel:
    id: str
    name: str
    protocol: str
    context: int
    output: int
    efforts: tuple[str, ...] = ()
    input_limit_tokens: int | None = None

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


def parse_models(directory: object, available: set[str]) -> tuple[ZenModel, ...]:
    if not isinstance(directory, dict) or not isinstance(directory.get("opencode"), dict):
        raise ValueError("Zen directory is missing the provider")
    provider = directory["opencode"]
    if provider.get("api", ZEN_BASE_URL) != ZEN_BASE_URL:
        raise ValueError("Zen directory changed its API destination")
    models = provider.get("models")
    if not isinstance(models, dict):
        raise ValueError("Zen directory has malformed models")
    result: list[ZenModel] = []
    for identity, model in models.items():
        if not isinstance(identity, str) or identity not in available or not isinstance(model, dict):
            continue
        cost = model.get("cost") or {}
        if not isinstance(cost, dict) or any(type(cost.get(key)) not in {int, float} or cost[key] != 0 for key in ("input", "output")):
            continue
        if model.get("tool_call") is not True:
            continue
        implementation = model.get("provider") or {}
        if not isinstance(implementation, dict):
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
        efforts = tuple(value for option in options if isinstance(option, dict) and option.get("type") == "effort"
                        and isinstance(option.get("values"), list) for value in option["values"]
                        if isinstance(value, str) and value in {"minimal", "low", "medium", "high", "xhigh", "max"})
        result.append(ZenModel(identity, str(model.get("name") or identity), protocol, context, output, efforts, input_limit))
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
        self.stale = False
        self.error = ""
        try:
            cache = read_json_strict(self.path)
            if isinstance(cache, dict) and cache.get("schema_version") == 1:
                self.models = parse_models(cache["directory"], set(cache["available"]))
                self._directory = cache["directory"]
                self.etag = cache["etag"]
                self.updated = float(cache["updated"])
        except (StoreCorruption, KeyError, ValueError, TypeError):
            self.stale = True

    def refresh(self, *, force: bool = False) -> tuple[ZenModel, ...]:
        with self._lock:
            if not force and time.time() - self.updated < 60:
                return self.models
            try:
                directory, etag = self.fetch(DIRECTORY_URL, self.etag)
                directory = directory if directory is not None else self._directory
                listing, _ = self.fetch(ZEN_BASE_URL + "/models", "")
                if not isinstance(listing, dict) or not isinstance(listing.get("data"), list):
                    raise ValueError("Zen endpoint returned a malformed listing")
                available = {row["id"] for row in listing["data"] if isinstance(row, dict) and isinstance(row.get("id"), str)}
                models = parse_models(directory, available)
                snapshot = {"opencode": directory["opencode"]}
                updated = time.time()
                write_json_atomic(self.path, {"schema_version": 1, "directory": snapshot, "available": sorted(available),
                                              "etag": etag, "updated": updated})
                self._directory, self.models, self.etag, self.updated = snapshot, models, etag, updated
                self.stale, self.error = False, ""
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self.stale, self.error = True, str(exc)[:240]
            return self.models

    def require(self, model_id: str, *, protocol: str = "") -> ZenModel:
        for model in self.refresh():
            if model.id == model_id and (not protocol or model.protocol == protocol):
                return model
        model = self._directory.get("opencode", {}).get("models", {}).get(model_id)
        if isinstance(model, dict) and _model_limits(model) is None:
            raise ValueError("selected Zen model is missing valid context and output limits")
        raise ValueError("selected model is not currently listed as a supported free Zen model")
