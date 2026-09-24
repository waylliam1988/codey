"""Local endpoint discovery: candidates, probes, resolution.

Owns the well-known OpenAI-compatible candidates (LM Studio, Ollama,
KoboldCPP, generic) and the ``/models`` probe. Probing runs in parallel so a
growing candidate list never slows cold start. ``local_openai.py`` keeps only
the provider runtime.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
PROBE_TIMEOUT = 1.5
DETECT_TIMEOUT = 0.6
DETECT_WORKERS = 4
# Memory bound only (not a total time guarantee) for /models probes.
MODELS_RESPONSE_MAX_BYTES = 1 * 1024 * 1024


@dataclass(frozen=True)
class LocalEndpoint:
    base_url: str
    models: tuple[str, ...] = ()

    @property
    def default_model(self) -> str:
        return self.models[0] if self.models else ""


@dataclass(frozen=True)
class LocalEndpointCandidate:
    kind: str
    label: str
    base_url: str


LOCAL_ENDPOINT_CANDIDATES: tuple[LocalEndpointCandidate, ...] = (
    LocalEndpointCandidate("lmstudio", "LM Studio", "http://127.0.0.1:1234/v1"),
    LocalEndpointCandidate("ollama", "Ollama", "http://127.0.0.1:11434/v1"),
    LocalEndpointCandidate("koboldcpp", "KoboldCPP", "http://127.0.0.1:5001/v1"),
    LocalEndpointCandidate("generic", "OpenAI compatible", "http://127.0.0.1:8080/v1"),
)

LOCAL_BASE_URL_CANDIDATES: tuple[str, ...] = tuple(c.base_url for c in LOCAL_ENDPOINT_CANDIDATES)


@dataclass(frozen=True)
class LocalEndpointProbe:
    candidate: LocalEndpointCandidate
    endpoint: LocalEndpoint | None
    reason: str


def probe_local_endpoint_detail(
    base_url: str,
    *,
    api_key: str = "",
    timeout: float = PROBE_TIMEOUT,
) -> tuple[LocalEndpoint | None, str]:
    """Probe /models and report why it failed without raising.

    Returns (endpoint, reason) where reason is one of
    ok/unreachable/auth/invalid_json.
    """
    url = (base_url or DEFAULT_BASE_URL).rstrip("/")
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(f"{url}/models", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MODELS_RESPONSE_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return None, "auth"
        return None, "unreachable"
    except Exception:
        return None, "unreachable"
    if len(raw) > MODELS_RESPONSE_MAX_BYTES:
        return None, "invalid_json"
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid_json"
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return None, "invalid_json"
    models = tuple(
        str(item.get("id"))
        for item in data
        if isinstance(item, dict) and item.get("id")
    )
    return LocalEndpoint(url, models), "ok"


def probe_local_endpoint(
    base_url: str,
    *,
    api_key: str = "",
    timeout: float = PROBE_TIMEOUT,
) -> LocalEndpoint | None:
    """Return an endpoint only for a valid OpenAI-compatible /models reply."""
    endpoint, reason = probe_local_endpoint_detail(base_url, api_key=api_key, timeout=timeout)
    return endpoint if reason == "ok" else None


def _probe_candidate(candidate: LocalEndpointCandidate, api_key: str, timeout: float) -> LocalEndpointProbe:
    endpoint, reason = probe_local_endpoint_detail(candidate.base_url, api_key=api_key, timeout=timeout)
    return LocalEndpointProbe(candidate=candidate, endpoint=endpoint, reason=reason)


def detect_local_endpoint_probes(
    *,
    api_key: str = "",
    timeout: float = DETECT_TIMEOUT,
    max_workers: int = DETECT_WORKERS,
) -> list[LocalEndpointProbe]:
    """Probe every candidate in parallel, preserving candidate order."""
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        return list(pool.map(
            lambda candidate: _probe_candidate(candidate, api_key, timeout),
            LOCAL_ENDPOINT_CANDIDATES,
        ))


def detect_local_endpoints(*, api_key: str = "", timeout: float = DETECT_TIMEOUT) -> list[LocalEndpoint]:
    """Return reachable endpoints in candidate order."""
    return [
        probe.endpoint
        for probe in detect_local_endpoint_probes(api_key=api_key, timeout=timeout)
        if probe.endpoint is not None
    ]


def default_local_base_url() -> str:
    import os

    from codey.env_names import LOCAL_OPENAI_BASE_URL_ENV
    from codey.providers.local_config import load_local_config as _load_config

    configured = os.environ.get(LOCAL_OPENAI_BASE_URL_ENV, "").strip()
    if configured:
        return configured
    remembered = _load_config().base_url
    if remembered:
        return remembered
    for probe in detect_local_endpoint_probes():
        if probe.endpoint is not None:
            return probe.endpoint.base_url
    return DEFAULT_BASE_URL


def local_endpoint_available(base_url: str = "") -> bool:
    import os

    from codey.env_names import LOCAL_OPENAI_API_KEY_ENV as _KEY_ENV
    from codey.env_names import LOCAL_OPENAI_BASE_URL_ENV as _URL_ENV
    from codey.providers.local_config import load_local_config as _load_config

    config = _load_config()
    remembered_key = config.api_key
    if base_url:
        return probe_local_endpoint(base_url, api_key=remembered_key) is not None
    configured = os.environ.get(_URL_ENV, "").strip()
    if configured:
        return probe_local_endpoint(configured, api_key=os.environ.get(_KEY_ENV, "")) is not None
    return resolve_local_endpoint(base_url=config.base_url, model=config.model, api_key=config.api_key) is not None


def resolve_local_endpoint(*, base_url: str = "", model: str = "", api_key: str = "") -> LocalEndpoint | None:
    """Resolve the remembered endpoint first, else the first reachable candidate."""
    import os

    from codey.env_names import LOCAL_OPENAI_API_KEY_ENV as _API_KEY_ENV

    remembered = (base_url or "").strip()
    if remembered:
        endpoint = probe_local_endpoint(remembered, api_key=api_key)
        if endpoint is not None:
            wanted = (model or endpoint.default_model or "").strip()
            models = ((wanted,) if wanted else ()) + tuple(m for m in endpoint.models if m != wanted)
            return LocalEndpoint(endpoint.base_url, models)
    detected = detect_local_endpoints(api_key=api_key or os.environ.get(_API_KEY_ENV, ""))
    return detected[0] if detected else None


__all__ = [
    "DEFAULT_BASE_URL",
    "DETECT_TIMEOUT",
    "DETECT_WORKERS",
    "LOCAL_BASE_URL_CANDIDATES",
    "LOCAL_ENDPOINT_CANDIDATES",
    "MODELS_RESPONSE_MAX_BYTES",
    "PROBE_TIMEOUT",
    "LocalEndpoint",
    "LocalEndpointCandidate",
    "LocalEndpointProbe",
    "default_local_base_url",
    "detect_local_endpoint_probes",
    "detect_local_endpoints",
    "local_endpoint_available",
    "probe_local_endpoint",
    "probe_local_endpoint_detail",
    "resolve_local_endpoint",
]
