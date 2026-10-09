"""KoboldCpp capability admission and full-template counting, owned by Local."""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any

from codey.providers.api_metering import RequestCounter, estimate_request
from codey.providers.api_transport import NoGenerationRedirect
from codey.providers.error_classification import RequestPrepError
from codey.providers.local_selection import _metadata_json
from codey.providers.token_accounting import RequestContextCount


def kobold_root(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.path.rstrip("/") != "/v1" or parsed.query or parsed.fragment:
        raise ValueError("KoboldCpp requires a confirmed /v1 connection")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def admitted_counter(base_url: str, api_key: str, protocol: str, window: int) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.path.rstrip("/") != "/v1" or protocol != "openai-completions":
        return "estimated", window
    root = kobold_root(base_url)
    version = _metadata_json(root + "/api/extra/version", api_key)
    if version.get("result") != "KoboldCpp":
        return "estimated", window
    if version.get("jinja") is not True:
        raise ValueError("KoboldCpp full-request counting requires its Jinja chat template")
    maximum = _metadata_json(root + "/api/extra/true_max_context_length", api_key).get("value")
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("KoboldCpp did not report a valid running context capacity")
    return "koboldcpp", min(window, maximum)


def counter_for(identity: str, base_url: str, model: str, api_key: str, window: int) -> RequestCounter:
    if identity == "koboldcpp":
        return KoboldRequestCounter(base_url, model, api_key, window)
    if identity == "estimated":
        return estimate_request
    raise ValueError("unsupported Local request counter")


class KoboldRequestCounter:
    def __init__(self, base_url: str, model: str, api_key: str, window: int) -> None:
        self.root = kobold_root(base_url)
        self.model, self.api_key, self.window = model, api_key, window

    def _json(self, path: str, deadline: float, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RequestPrepError("KoboldCpp token counting deadline exceeded")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.root + path, headers=headers,
                                         data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None)
        try:
            with urllib.request.build_opener(NoGenerationRedirect()).open(request, timeout=min(remaining, 5.0)) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("token count response exceeded its byte limit")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("token count response must be an object")
            return result
        except (OSError, ValueError) as exc:
            raise RequestPrepError(f"KoboldCpp token counting failed: {exc}") from exc

    def __call__(self, payload: dict[str, Any], *, deadline: float) -> RequestContextCount:
        if self._json("/api/v1/model", deadline).get("result") != self.model:
            raise RequestPrepError("KoboldCpp loaded model changed; select the model again")
        maximum = self._json("/api/extra/true_max_context_length", deadline).get("value")
        if type(maximum) is not int or maximum < self.window:
            raise RequestPrepError("KoboldCpp running context capacity changed; select the model again")
        result = self._json("/api/extra/tokencount", deadline, payload)
        value = result.get("value")
        if type(value) is not int or value < 0:
            raise RequestPrepError("KoboldCpp returned an invalid token count")
        return RequestContextCount(value, "tokenizer")
