"""Local endpoint discovery: candidates, probes, resolution.

Owns the well-known OpenAI-compatible candidates (LM Studio, Ollama,
KoboldCPP, generic) and the ``/models`` probe. Probing runs in parallel so a
growing candidate list never slows cold start. ``api_provider.py`` keeps only
the provider runtime.
"""

from __future__ import annotations

import contextlib
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
        with contextlib.suppress(Exception):
            exc.close()
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
    def probe(candidate: LocalEndpointCandidate) -> LocalEndpointProbe:
        return _probe_candidate(candidate, api_key, timeout)

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        return list(pool.map(
            probe,
            LOCAL_ENDPOINT_CANDIDATES,
        ))


def detect_local_endpoints(*, api_key: str = "", timeout: float = DETECT_TIMEOUT) -> list[LocalEndpoint]:
    """Return reachable endpoints in candidate order."""
    return [
        probe.endpoint
        for probe in detect_local_endpoint_probes(api_key=api_key, timeout=timeout)
        if probe.endpoint is not None
    ]


def resolve_local_endpoint(*, base_url: str = "", model: str = "", api_key: str = "") -> LocalEndpoint | None:
    """Probe one explicit address, or auto-discover only when none is set.

    An explicit address never falls back to another service: failure
    returns None so saved credentials are not carried elsewhere. Without
    an address, discovery never broadcasts a key, and a requested model
    selects the endpoint that provides it (no silent switch to another
    model).
    """
    remembered = (base_url or "").strip()
    if remembered:
        endpoint = probe_local_endpoint(remembered, api_key=api_key)
        if endpoint is None:
            return None
        wanted = (model or endpoint.default_model or "").strip()
        models = ((wanted,) if wanted else ()) + tuple(m for m in endpoint.models if m != wanted)
        return LocalEndpoint(endpoint.base_url, models)
    wanted = (model or "").strip()
    for endpoint in detect_local_endpoints(api_key=""):
        if not wanted:
            return endpoint
        if wanted in endpoint.models:
            models = (wanted,) + tuple(m for m in endpoint.models if m != wanted)
            return LocalEndpoint(endpoint.base_url, models)
    return None


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
    "detect_local_endpoint_probes",
    "detect_local_endpoints",
    "probe_local_endpoint",
    "probe_local_endpoint_detail",
    "resolve_local_endpoint",
]
