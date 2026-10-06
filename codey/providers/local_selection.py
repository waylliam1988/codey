"""Bounded model metadata and immutable per-chat local run selection."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from typing import Any

from codey.providers.local_config import LocalProviderConfig, load_local_config, select_local_target


def thinking_options_from_metadata(props: object, version: object, model: str) -> list[str]:
    if not isinstance(props, dict) or not isinstance(version, dict):
        return []
    # A returned reasoning field alone is not a capability. Kobold explicitly
    # reports the active Jinja engine; its template must consume this switch.
    if version.get("result") != "KoboldCpp" or version.get("jinja") is not True:
        return []
    if props.get("model_path") != model:
        return []
    template = props.get("chat_template")
    if not isinstance(template, str) or not re.search(r"\benable_thinking\b", template):
        return []
    version_text = str(version.get("version", ""))
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version_text)
    if match and (1, 112, 2) <= tuple(map(int, match.groups())) < (1, 118, 0):
        return ["off", "minimal", "low", "medium", "high"]
    return ["off", "high"]


def _metadata_json(url: str, api_key: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
    # Never forward a saved credential across an HTTP redirect.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any) -> None:
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=0.8) as response:
            raw = response.read(262145)
        if len(raw) > 262144:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def model_metadata(base_url: str, model: str, *, api_key: str = "", display_name: str = "") -> dict[str, object]:
    leaf = model.rsplit("/", 1)[-1]
    short = re.match(r"^([A-Za-z][\w.]*?)[-_](\d+(?:\.\d+)?[Bb])(?=[-_]|$)", leaf)
    name = display_name or (f"{short[1]} {short[2].upper()}" if short else leaf) or "Local"
    options: list[str] = []
    parsed = urllib.parse.urlsplit(base_url)
    # Only the standard /v1 sibling endpoints have a known metadata contract;
    # arbitrary reverse-proxy paths must not receive credentials at their root.
    if parsed.scheme in {"http", "https"} and parsed.path.rstrip("/") == "/v1":
        root = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        version = _metadata_json(root + "/api/extra/version", api_key)
        if version.get("result") == "KoboldCpp" and version.get("jinja") is True:
            options = thinking_options_from_metadata(_metadata_json(root + "/props", api_key), version, model)
    return {"display_name": name, "thinking_options": options}


def capture_local_run_config(selection: object) -> LocalProviderConfig:
    saved = load_local_config()
    target = select_local_target(saved)
    if not isinstance(selection, dict):
        raise ValueError("local selection must be an object")
    base = str(selection.get("base_url") or "").rstrip("/")
    expected_base = target.base_url
    if not expected_base:
        from codey.providers.local_discovery import resolve_local_endpoint

        endpoint = resolve_local_endpoint(model=target.model, api_key="")
        expected_base = endpoint.base_url if endpoint is not None else ""
    if not base or base != expected_base:
        raise ValueError("Local connection changed; select the model again.")
    model = selection.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 1000:
        raise ValueError("model required")
    thinking = selection.get("thinking")
    if thinking is not None and type(thinking) is not bool:
        raise ValueError("thinking must be true, false, or null")
    effort = selection.get("effort")
    if effort is not None and (not isinstance(effort, str) or effort not in {"off", "minimal", "low", "medium", "high", "max"}):
        raise ValueError("unsupported thinking effort")
    return replace(saved, base_url=base, model=model.strip(), api_key=target.api_key,
                   thinking_enabled=effort != "off" if effort is not None else thinking,
                   reasoning_effort=effort)
