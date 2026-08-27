"""OpenAI-compatible local model provider."""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from codey.storage.local_store import DEFAULT_STATE_HOME, read_json, write_json_atomic

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
LOCAL_BASE_URL_CANDIDATES = (
    "http://127.0.0.1:1234/v1",
    "http://127.0.0.1:11434/v1",
    "http://127.0.0.1:8080/v1",
)
DEFAULT_TIMEOUT = 180
DEFAULT_TEMPERATURE = 0.3
LOCAL_BASE_URL_ENV = "CODEY_LOCAL_OPENAI_BASE_URL"
LOCAL_MODEL_ENV = "CODEY_LOCAL_OPENAI_MODEL"
LOCAL_API_KEY_ENV = "CODEY_LOCAL_OPENAI_API_KEY"
_RESPONSE_PREVIEW_LIMIT = 400
_RESPONSE_RETRIES = 1
_CONFIG_FILE = "local-openai.json"


@dataclass(frozen=True)
class LocalEndpoint:
    base_url: str
    models: tuple[str, ...] = ()

    @property
    def default_model(self) -> str:
        return self.models[0] if self.models else ""


class _RetryableResponseError(RuntimeError):
    pass


class LocalOpenAIProvider:
    name = "Local"
    thread_safe_send = True

    def __init__(
        self,
        base_url: str = "",
        model: str = "",
        *,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        temperature: float = DEFAULT_TEMPERATURE,
        system_prompt: str = "",
    ) -> None:
        self.base_url = (base_url or os.environ.get(LOCAL_BASE_URL_ENV) or default_local_base_url()).rstrip("/")
        self.model = model or os.environ.get(LOCAL_MODEL_ENV) or "local-model"
        self.api_key = api_key or os.environ.get(LOCAL_API_KEY_ENV, "")
        self.timeout = timeout
        self.temperature = temperature
        self.system_prompt = system_prompt
        self._messages: list[dict] = []

    @classmethod
    def connect(cls, **_kwargs) -> "LocalOpenAIProvider":
        endpoint = resolve_local_endpoint()
        if endpoint is None:
            return cls()
        config = load_local_config()
        model = str(config.get("model") or endpoint.default_model or "")
        api_key = str(config.get("api_key") or "")
        return cls(endpoint.base_url, model, api_key=api_key)

    @property
    def location(self) -> str:
        return f"{self.base_url} ({self.model})"

    def new_chat(self, timeout: float | None = None) -> None:
        self._messages = []

    def send(self, text: str, timeout: float | None = None) -> str:
        if not self._messages and self.system_prompt:
            self._messages.append({"role": "system", "content": self.system_prompt})
        self._messages.append({"role": "user", "content": text})
        reply = self._complete(self._messages, timeout=timeout)
        self._messages.append({"role": "assistant", "content": reply})
        return reply

    def close(self) -> None:
        self._messages = []

    def _complete(self, messages: list[dict], *, timeout: float | None = None) -> str:
        endpoint = f"{self.base_url}/chat/completions"
        payload = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
                "stream": False,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers=headers,
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(_RESPONSE_RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                    raw = response.read()
                body = _load_response_json(raw, endpoint)
                return _extract_reply(body)
            except http.client.IncompleteRead as exc:
                last_error = _RetryableResponseError(
                    f"local model at {endpoint} returned a truncated response "
                    f"({len(exc.partial)} bytes read, {exc.expected} more expected)"
                )
            except _RetryableResponseError as exc:
                last_error = exc
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
                raise RuntimeError(f"local model HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise RuntimeError(f"could not reach local model at {self.base_url}: {exc}") from exc
            if attempt < _RESPONSE_RETRIES:
                continue
        raise RuntimeError(str(last_error or f"local model at {endpoint} did not return a reply"))


def default_local_base_url() -> str:
    configured = os.environ.get(LOCAL_BASE_URL_ENV, "").strip()
    if configured:
        return configured
    remembered = load_local_config().get("base_url")
    if remembered:
        return str(remembered)
    for candidate in LOCAL_BASE_URL_CANDIDATES:
        if local_endpoint_available(candidate):
            return candidate
    return DEFAULT_BASE_URL


def local_endpoint_available(base_url: str = "") -> bool:
    config = load_local_config()
    remembered_key = str(config.get("api_key") or "")
    if base_url:
        return probe_local_endpoint(base_url, api_key=remembered_key) is not None
    configured = os.environ.get(LOCAL_BASE_URL_ENV, "").strip()
    if configured:
        return probe_local_endpoint(configured, api_key=os.environ.get(LOCAL_API_KEY_ENV, "")) is not None
    endpoint = resolve_local_endpoint()
    return endpoint is not None


def probe_local_endpoint(
    base_url: str,
    *,
    api_key: str = "",
    timeout: float = 1.5,
) -> LocalEndpoint | None:
    url = (base_url or DEFAULT_BASE_URL).rstrip("/")
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(f"{url}/models", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except Exception:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = {}
    data = body.get("data") if isinstance(body, dict) else None
    models = tuple(
        str(item.get("id"))
        for item in (data or [])
        if isinstance(item, dict) and item.get("id")
    )
    return LocalEndpoint(url, models)


def detect_local_endpoints(*, api_key: str = "") -> list[LocalEndpoint]:
    found: list[LocalEndpoint] = []
    seen: set[str] = set()
    for candidate in LOCAL_BASE_URL_CANDIDATES:
        normalized = candidate.rstrip("/")
        if normalized in seen:
            continue
        seen.add(normalized)
        endpoint = probe_local_endpoint(normalized, api_key=api_key)
        if endpoint is not None:
            found.append(endpoint)
    return found


def resolve_local_endpoint() -> LocalEndpoint | None:
    config = load_local_config()
    remembered = str(config.get("base_url") or "").strip()
    api_key = str(config.get("api_key") or "")
    if remembered:
        endpoint = probe_local_endpoint(remembered, api_key=api_key)
        if endpoint is not None:
            model = str(config.get("model") or endpoint.default_model or "")
            models = ((model,) if model else ()) + tuple(m for m in endpoint.models if m != model)
            return LocalEndpoint(endpoint.base_url, models)
    detected = detect_local_endpoints(api_key=os.environ.get(LOCAL_API_KEY_ENV, ""))
    return detected[0] if detected else None


def load_local_config() -> dict:
    return read_json(_config_path()) or {}


def save_local_config(
    base_url: str,
    model: str = "",
    api_key: str | None = None,
) -> None:
    previous = load_local_config()
    if api_key is None:
        stored_key = str(previous.get("api_key") or "").strip()
    else:
        stored_key = str(api_key or "").strip()
    payload = {
        "base_url": (base_url or "").strip().rstrip("/"),
        "model": (model or "").strip(),
        "api_key": stored_key,
    }
    write_json_atomic(_config_path(), payload, mode=0o600)


def local_config_payload() -> dict:
    config = load_local_config()
    endpoint = resolve_local_endpoint()
    api_key = str(config.get("api_key") or "")
    candidates = detect_local_endpoints(api_key=api_key) if endpoint is None else []
    model = ""
    if endpoint is not None:
        model = endpoint.default_model
    model = str(config.get("model") or model or "")
    return {
        "connected": endpoint is not None,
        "base_url": (endpoint.base_url if endpoint is not None else str(config.get("base_url") or "")),
        "model": model,
        "models": list(endpoint.models if endpoint is not None else ()),
        "candidates": [item.base_url for item in candidates],
        "has_api_key": bool(config.get("api_key")),
    }


def _config_path() -> Path:
    return DEFAULT_STATE_HOME / _CONFIG_FILE


def _extract_reply(body: dict) -> str:
    try:
        choice = body["choices"][0]
    except (KeyError, IndexError, TypeError):
        return ""
    message = choice.get("message") if isinstance(choice, dict) else None
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(choice.get("text") or "") if isinstance(choice, dict) else ""


def _load_response_json(raw: bytes, endpoint: str) -> dict:
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        raise _RetryableResponseError(
            f"local model at {endpoint} returned an empty response; expected OpenAI-compatible JSON"
        )
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        preview = " ".join(text.split())[:_RESPONSE_PREVIEW_LIMIT]
        raise RuntimeError(
            f"local model at {endpoint} returned non-JSON response: {preview}"
        ) from exc
    if not isinstance(body, dict):
        raise RuntimeError(
            f"local model at {endpoint} returned a non-object JSON response"
        )
    return body
