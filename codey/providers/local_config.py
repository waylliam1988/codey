"""Canonical local-model configuration (single source of truth).

Owns the ``local-openai.json`` shape, context-budget math, native-tools
mode, and the bootstrap payload. ``local_openai.py`` keeps only the provider
runtime; ``app/api.py`` parses through this module instead of understanding
local config itself.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from codey.env_names import (
    LOCAL_OPENAI_API_KEY_ENV,
    LOCAL_OPENAI_BASE_URL_ENV,
    LOCAL_OPENAI_CONTEXT_KEEP_ENV,
    LOCAL_OPENAI_CONTEXT_RESERVE_ENV,
    LOCAL_OPENAI_CONTEXT_WINDOW_ENV,
    LOCAL_OPENAI_MODEL_ENV,
    NATIVE_TOOLS_ENV,
)
from codey.storage.local_store import (
    DEFAULT_STATE_HOME,
    StoreCorruption,
    backup_corrupt_file,
    read_json_strict,
    write_json_atomic,
)

CONFIG_FILE = "local-openai.json"
SCHEMA_VERSION = 2

NATIVE_TOOLS_AUTO = "auto"
NATIVE_TOOLS_ON = "on"
NATIVE_TOOLS_OFF = "off"
NATIVE_TOOLS_MODES = (NATIVE_TOOLS_AUTO, NATIVE_TOOLS_ON, NATIVE_TOOLS_OFF)

CONTEXT_PRESETS: tuple[tuple[str, str, int], ...] = (
    ("32k", "32k", 32_768),
    ("128k", "128k", 131_072),
    ("262k", "262k", 262_144),
)


@dataclass(frozen=True)
class LocalContextBudget:
    context_window_tokens: int
    context_reserve_tokens: int
    context_keep_recent_tokens: int
    source: str = "default"


@dataclass(frozen=True)
class LocalProviderConfig:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    native_tools_mode: str = NATIVE_TOOLS_AUTO
    context: LocalContextBudget | None = None


@dataclass(frozen=True)
class EffectiveLocalConfig:
    base_url: str
    model: str
    api_key: str
    native_tools: bool
    context: LocalContextBudget


@dataclass(frozen=True)
class LocalTargetSelection:
    """One grouped target: address, model, and key stay together."""

    base_url: str
    model: str
    api_key: str
    source: str = ""


def select_local_target(
    config: LocalProviderConfig | None = None,
) -> LocalTargetSelection:
    """Decide the single local target once (address + model + key).

    Priority keeps each group together so a saved key is never carried to
    another service: an env base wins entirely on its own; only when the
    env and saved bases are the same target do env model/key fall back to
    the saved values. With no env base, the saved base wins with env
    model/key as same-target overrides. With no base, env model/key win
    if set, else the saved ones (discovery preference only).
    """
    loaded = config if config is not None else load_local_config()
    env_base = os.environ.get(LOCAL_OPENAI_BASE_URL_ENV, "").strip().rstrip("/")
    env_model = os.environ.get(LOCAL_OPENAI_MODEL_ENV, "").strip()
    env_key = os.environ.get(LOCAL_OPENAI_API_KEY_ENV, "").strip()
    cfg_base = loaded.base_url.strip().rstrip("/")
    cfg_model = loaded.model.strip()
    cfg_key = loaded.api_key.strip() if isinstance(loaded.api_key, str) else ""
    if env_base:
        # Conservative address compare: URL paths may be case-sensitive, so
        # only an exact match (ignoring one trailing slash) counts as the
        # same target for key fallback.
        if cfg_base and env_base.rstrip("/") == cfg_base.rstrip("/"):
            return LocalTargetSelection(
                base_url=env_base,
                model=env_model or cfg_model,
                api_key=env_key or cfg_key,
                source="env",
            )
        return LocalTargetSelection(
            base_url=env_base,
            model=env_model,
            api_key=env_key,
            source="env",
        )
    if cfg_base:
        return LocalTargetSelection(
            base_url=cfg_base,
            model=env_model or cfg_model,
            api_key=env_key or cfg_key,
            source="config",
        )
    return LocalTargetSelection(
        base_url="",
        model=env_model or cfg_model,
        api_key=env_key or cfg_key,
        source="",
    )


def context_budget_for_window(window_tokens: int) -> LocalContextBudget:
    """Derive reserve/keep from one window size (UI passes a single number)."""
    window = int(window_tokens)
    reserve = min(max(window // 8, 8_192), 32_768)
    keep = min(max(window // 8, 12_000), 32_000)
    return LocalContextBudget(window, reserve, keep, source="preset")


def validate_context_budget(budget: LocalContextBudget) -> str:
    """Return an error string, or "" when the budget is usable."""
    window = budget.context_window_tokens
    reserve = budget.context_reserve_tokens
    keep = budget.context_keep_recent_tokens
    if not (isinstance(window, int) and not isinstance(window, bool) and window > 0):
        return "context_window_tokens must be a positive integer"
    if not (isinstance(reserve, int) and not isinstance(reserve, bool) and reserve > 0):
        return "context_reserve_tokens must be a positive integer"
    if not (isinstance(keep, int) and not isinstance(keep, bool) and keep > 0):
        return "context_keep_recent_tokens must be a positive integer"
    if not window > reserve:
        return "context_window_tokens must be larger than context_reserve_tokens"
    if keep > window - reserve:
        return "context_keep_recent_tokens must not exceed context_window_tokens minus context_reserve_tokens"
    return ""


def _parse_bool_flag(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
    return None


def _parse_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    if isinstance(value, str):
        text = value.strip().replace("_", "").replace(",", "")
        if text.isdigit() and int(text) > 0:
            return int(text)
    return None


def _parse_optional_positive_int_field(body: Mapping[str, object], key: str) -> tuple[int | None, str]:
    """Missing/empty means not provided; non-empty but unparsable is a 400."""
    if key not in body:
        return None, ""
    raw = body.get(key)
    if raw is None or str(raw).strip() == "":
        return None, ""
    parsed = _parse_positive_int(raw)
    if parsed is None:
        return None, f"{key} must be a positive integer"
    return parsed, ""


def _config_path() -> Path:
    return DEFAULT_STATE_HOME / CONFIG_FILE


def _default_budget() -> LocalContextBudget:
    """Capability is the single source of default budgets (never hardcodes)."""
    from codey.providers.capabilities import capability_for

    capability = capability_for("local")
    return LocalContextBudget(
        int(capability.context_window_tokens),
        int(capability.context_reserve_tokens),
        int(capability.context_keep_recent_tokens),
        source="default",
    )


def parse_native_tools_mode(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in NATIVE_TOOLS_MODES:
        return text
    return None


def config_from_dict(raw: object) -> LocalProviderConfig:
    """Coerce a schema-2 mapping to canonical config."""
    if not isinstance(raw, dict):
        return LocalProviderConfig()
    mode = parse_native_tools_mode(raw.get("native_tools_mode")) or NATIVE_TOOLS_AUTO
    context_raw = raw.get("context")
    context: LocalContextBudget | None = None
    if isinstance(context_raw, dict):
        window = _parse_positive_int(context_raw.get("context_window_tokens"))
        reserve = _parse_positive_int(context_raw.get("context_reserve_tokens"))
        keep = _parse_positive_int(context_raw.get("context_keep_recent_tokens"))
        if window is not None and reserve is not None and keep is not None:
            candidate = LocalContextBudget(window, reserve, keep, source="config")
            if not validate_context_budget(candidate):
                context = candidate
    return LocalProviderConfig(
        base_url=str(raw.get("base_url") or "").strip(),
        model=str(raw.get("model") or "").strip(),
        api_key=str(raw.get("api_key") or ""),
        native_tools_mode=mode,
        context=context,
    )


def load_local_config() -> LocalProviderConfig:
    """Read the canonical config file."""
    try:
        raw = read_json_strict(_config_path()) or {}
    except StoreCorruption:
        backup_corrupt_file(_config_path())
        return LocalProviderConfig()
    return config_from_dict(raw)


def config_to_payload(config: LocalProviderConfig) -> dict[str, object]:
    """Render the canonical schema-2 payload without touching disk."""
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "base_url": config.base_url.strip().rstrip("/"),
        "model": config.model.strip(),
        "api_key": config.api_key,
        "native_tools_mode": config.native_tools_mode,
    }
    if config.context is not None:
        payload["context"] = {
            "context_window_tokens": int(config.context.context_window_tokens),
            "context_reserve_tokens": int(config.context.context_reserve_tokens),
            "context_keep_recent_tokens": int(config.context.context_keep_recent_tokens),
        }
    return payload


def save_local_config(config: LocalProviderConfig) -> None:
    """Persist the canonical shape (always schema 2)."""
    write_json_atomic(_config_path(), config_to_payload(config), mode=0o600)


def parse_local_config_update(
    body: Mapping[str, object],
    previous: LocalProviderConfig,
) -> tuple[LocalProviderConfig | None, str]:
    """Parse an API/UI update body against the previous config.

    The UI passes at most one context number; reserve/keep derive from the
    preset unless explicitly overridden. Returns (config, "") or (None, error).
    """
    if not isinstance(body, Mapping):
        return None, "request body must be an object"
    base_url = str(body.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        return None, "base_url required"
    model = str(body.get("model") or "").strip()
    raw_key = body.get("api_key")
    # Empty means "not provided": the API layer reuses the stored key only
    # for the same target, and refuses a target change without an explicit key.
    api_key = "" if raw_key is None else str(raw_key).strip()
    mode = parse_native_tools_mode(body.get("native_tools_mode"))
    if mode is None and "native_tools_mode" in body:
        return None, "native_tools_mode must be auto, on, or off"
    if mode is None:
        mode = previous.native_tools_mode
    window, window_error = _parse_optional_positive_int_field(body, "context_window_tokens")
    if window_error:
        return None, window_error
    reserve, reserve_error = _parse_optional_positive_int_field(body, "context_reserve_tokens")
    if reserve_error:
        return None, reserve_error
    keep, keep_error = _parse_optional_positive_int_field(body, "context_keep_recent_tokens")
    if keep_error:
        return None, keep_error
    if window is None and (reserve is not None or keep is not None):
        return None, (
            "context_window_tokens required when overriding "
            "context_reserve_tokens or context_keep_recent_tokens"
        )
    context = previous.context
    if window is not None:
        preset = context_budget_for_window(window)
        context = LocalContextBudget(
            window,
            reserve if reserve is not None else preset.context_reserve_tokens,
            keep if keep is not None else preset.context_keep_recent_tokens,
            source="config",
        )
        error = validate_context_budget(context)
        if error:
            return None, error
    return (
        LocalProviderConfig(
            base_url=base_url,
            model=model,
            api_key=api_key,
            native_tools_mode=mode,
            context=context,
        ),
        "",
    )


def resolve_local_native_tools(config: LocalProviderConfig) -> bool:
    """Env wins; then the stored mode (auto falls back to capability default)."""
    env_flag = _parse_bool_flag(os.environ.get(NATIVE_TOOLS_ENV, "").strip())
    if env_flag is not None:
        return env_flag
    if config.native_tools_mode == NATIVE_TOOLS_ON:
        return True
    if config.native_tools_mode == NATIVE_TOOLS_OFF:
        return False
    from codey.providers.capabilities import capability_for

    return bool(capability_for("local").native_tools_default)


def resolve_local_context_budget(config: LocalProviderConfig) -> LocalContextBudget:
    """Env fields win per-field; then stored context; then defaults.

    An explicitly set but unparsable or inconsistent env override is a
    configuration error (ValueError), never a silent fallback to defaults.
    Unset env falls back to the stored budget or capability defaults.
    """
    defaults = _default_budget()
    raw_window = os.environ.get(LOCAL_OPENAI_CONTEXT_WINDOW_ENV, "")
    raw_reserve = os.environ.get(LOCAL_OPENAI_CONTEXT_RESERVE_ENV, "")
    raw_keep = os.environ.get(LOCAL_OPENAI_CONTEXT_KEEP_ENV, "")
    window_set = bool(str(raw_window).strip())
    reserve_set = bool(str(raw_reserve).strip())
    keep_set = bool(str(raw_keep).strip())
    window = _parse_positive_int(str(raw_window).strip()) if window_set else None
    reserve = _parse_positive_int(str(raw_reserve).strip()) if reserve_set else None
    keep = _parse_positive_int(str(raw_keep).strip()) if keep_set else None
    if window_set and window is None:
        raise ValueError(f"{LOCAL_OPENAI_CONTEXT_WINDOW_ENV} must be a positive integer")
    if reserve_set and reserve is None:
        raise ValueError(f"{LOCAL_OPENAI_CONTEXT_RESERVE_ENV} must be a positive integer")
    if keep_set and keep is None:
        raise ValueError(f"{LOCAL_OPENAI_CONTEXT_KEEP_ENV} must be a positive integer")
    stored = config.context
    candidate = LocalContextBudget(
        window if window is not None else (stored.context_window_tokens if stored else defaults.context_window_tokens),
        reserve if reserve is not None else (stored.context_reserve_tokens if stored else defaults.context_reserve_tokens),
        keep if keep is not None else (stored.context_keep_recent_tokens if stored else defaults.context_keep_recent_tokens),
        source="config" if stored else defaults.source,
    )
    error = validate_context_budget(candidate)
    if error:
        if window_set or reserve_set or keep_set:
            raise ValueError(f"invalid local context budget from environment: {error}")
        return defaults
    if window_set or reserve_set or keep_set:
        return LocalContextBudget(
            candidate.context_window_tokens,
            candidate.context_reserve_tokens,
            candidate.context_keep_recent_tokens,
            source="env",
        )
    return candidate


def resolve_effective_local_config(
    config: LocalProviderConfig,
    selection: LocalTargetSelection,
    endpoint: object,
) -> EffectiveLocalConfig:
    """Pure view over an already-selected target and endpoint.

    No env reads, no discovery: callers select once, probe once, then
    compute. An explicit model is kept for an explicit base; for
    auto-discovery a preference applies only when present on the endpoint.
    """
    base_url = str(getattr(endpoint, "base_url", "") or "") if endpoint is not None else ""
    models = tuple(getattr(endpoint, "models", ()) or ()) if endpoint is not None else ()
    if selection.model:
        if endpoint is None or selection.model in models or selection.source != "":
            model = selection.model
        else:
            model = models[0] if models else selection.model
    else:
        model = models[0] if models else ""
    return EffectiveLocalConfig(
        base_url=base_url or selection.base_url,
        model=model,
        api_key=selection.api_key,
        native_tools=resolve_local_native_tools(config),
        context=resolve_local_context_budget(config),
    )


def local_bootstrap_payload() -> dict:
    """UI status: connection, models, native mode, context, presets."""
    config = load_local_config()
    selection = select_local_target(config)
    remembered = selection.base_url
    try:
        from codey.providers import local_discovery as discovery

        if remembered:
            endpoint = discovery.probe_local_endpoint(remembered, api_key=selection.api_key)
            if endpoint is not None:
                wanted = (selection.model or endpoint.default_model or "").strip()
                endpoint = discovery.LocalEndpoint(
                    endpoint.base_url,
                    ((wanted,) if wanted else ()) + tuple(m for m in endpoint.models if m != wanted),
                )
            if endpoint is not None:
                discovered: list[str] = []
            else:
                # Explicit target offline: list the other well-known
                # candidates as try-buttons without probing them with this
                # target's key, and never claim another service as connected.
                # Paths may be case-sensitive: compare addresses exactly.
                discovered = [
                    candidate.base_url
                    for candidate in discovery.LOCAL_ENDPOINT_CANDIDATES
                    if candidate.base_url.rstrip("/") != remembered.rstrip("/")
                ]
        else:
            # No selected target: one parallel pass without broadcasting a
            # key; a requested model selects its provider or stays offline.
            probes = discovery.detect_local_endpoint_probes(api_key="")
            reachable = [probe.endpoint for probe in probes if probe.endpoint is not None]
            endpoint = None
            if reachable:
                if selection.model:
                    for candidate_endpoint in reachable:
                        if selection.model in candidate_endpoint.models:
                            wanted = selection.model.strip()
                            endpoint = discovery.LocalEndpoint(
                                candidate_endpoint.base_url,
                                (wanted,) + tuple(m for m in candidate_endpoint.models if m != wanted),
                            )
                            break
                else:
                    endpoint = reachable[0]
            discovered = (
                []
                if endpoint is not None
                else [probe.candidate.base_url for probe in probes if probe.endpoint is None]
            )
    except Exception:
        endpoint = None
        discovered = []
    try:
        effective = resolve_effective_local_config(config, selection, endpoint)
        context_error = ""
        context_payload: dict[str, object] | None = asdict(effective.context)
        context_window = effective.context.context_window_tokens
        context_reserve = effective.context.context_reserve_tokens
        context_keep = effective.context.context_keep_recent_tokens
        native_tools = effective.native_tools
        display_base = effective.base_url or remembered
        display_model = effective.model
    except ValueError as exc:
        # Invalid env override is display-only here: report the error with
        # no budget (never a default that looks runnable). Runtime paths
        # raise through resolve_effective_local_config instead.
        context_error = str(exc)
        endpoint_base = str(getattr(endpoint, "base_url", "") or "") if endpoint is not None else ""
        endpoint_models = tuple(getattr(endpoint, "models", ()) or ()) if endpoint is not None else ()
        if selection.model and (endpoint is None or selection.model in endpoint_models or selection.source != ""):
            display_model = selection.model
        else:
            display_model = endpoint_models[0] if endpoint_models else selection.model
        display_base = endpoint_base or selection.base_url or remembered
        native_tools = resolve_local_native_tools(config)
        context_payload = None
        context_window = None
        context_reserve = None
        context_keep = None
    models: list[str] = []
    if endpoint is not None:
        models = list(endpoint.models)
    target_error = ""
    if endpoint is not None and not display_model:
        target_error = "local endpoint returned no usable model"
    connected = endpoint is not None and not context_error and not target_error
    return {
        "connected": connected,
        "base_url": display_base,
        "model": display_model,
        "models": models,
        "candidates": discovered,
        "has_api_key": bool(selection.api_key),
        "native_tools_mode": config.native_tools_mode,
        "native_tools": native_tools,
        "context": context_payload,
        "context_window_tokens": context_window,
        "context_reserve_tokens": context_reserve,
        "context_keep_recent_tokens": context_keep,
        "context_error": context_error,
        "error": target_error,
        "presets": [
            {"id": preset_id, "label": label, "window": window}
            for preset_id, label, window in CONTEXT_PRESETS
        ],
    }


__all__ = [
    "CONFIG_FILE",
    "CONTEXT_PRESETS",
    "NATIVE_TOOLS_AUTO",
    "NATIVE_TOOLS_MODES",
    "NATIVE_TOOLS_OFF",
    "NATIVE_TOOLS_ON",
    "SCHEMA_VERSION",
    "EffectiveLocalConfig",
    "LocalContextBudget",
    "LocalProviderConfig",
    "LocalTargetSelection",
    "config_from_dict",
    "config_to_payload",
    "context_budget_for_window",
    "load_local_config",
    "local_bootstrap_payload",
    "parse_local_config_update",
    "parse_native_tools_mode",
    "resolve_effective_local_config",
    "resolve_local_context_budget",
    "resolve_local_native_tools",
    "save_local_config",
    "select_local_target",
    "validate_context_budget",
]
