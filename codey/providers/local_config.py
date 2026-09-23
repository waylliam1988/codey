"""Canonical local-model configuration (single source of truth).

Owns the ``local-openai.json`` shape, context-budget math, native-tools
mode, and the bootstrap payload. ``local_openai.py`` keeps only the provider
runtime plus thin facades; ``app/api.py`` parses through this module instead
of understanding local config itself.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from codey.env_names import (
    LOCAL_OPENAI_CONTEXT_KEEP_ENV,
    LOCAL_OPENAI_CONTEXT_RESERVE_ENV,
    LOCAL_OPENAI_CONTEXT_WINDOW_ENV,
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

_DEFAULT_WINDOW = 32_768
_DEFAULT_RESERVE = 8_192
_DEFAULT_KEEP = 12_000


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


def _config_path() -> Path:
    return DEFAULT_STATE_HOME / CONFIG_FILE


def _default_budget() -> LocalContextBudget:
    try:
        from codey.providers.capabilities import capability_for

        capability = capability_for("local")
        return LocalContextBudget(
            int(capability.context_window_tokens),
            int(capability.context_reserve_tokens),
            int(capability.context_keep_recent_tokens),
            source="default",
        )
    except Exception:
        return LocalContextBudget(_DEFAULT_WINDOW, _DEFAULT_RESERVE, _DEFAULT_KEEP, source="default")


def parse_native_tools_mode(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in NATIVE_TOOLS_MODES:
        return text
    flag = _parse_bool_flag(value)
    if flag is True:
        return NATIVE_TOOLS_ON
    if flag is False:
        return NATIVE_TOOLS_OFF
    return None


def config_from_dict(raw: object) -> LocalProviderConfig:
    """Coerce a raw mapping to canonical config, migrating legacy fields."""
    if not isinstance(raw, dict):
        return LocalProviderConfig()
    mode = parse_native_tools_mode(raw.get("native_tools_mode"))
    if mode is None:
        # One-time migration read of the legacy flat field.
        legacy_flag = _parse_bool_flag(raw.get("native_tools"))
        mode = NATIVE_TOOLS_ON if legacy_flag else NATIVE_TOOLS_OFF if legacy_flag is False else NATIVE_TOOLS_AUTO
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
    if context is None:
        # One-time migration read of the legacy flat fields.
        window = _parse_positive_int(raw.get("context_window_tokens"))
        if window is not None:
            reserve = _parse_positive_int(raw.get("context_reserve_tokens"))
            keep = _parse_positive_int(raw.get("context_keep_recent_tokens"))
            if reserve is not None and keep is not None:
                candidate = LocalContextBudget(window, reserve, keep, source="config")
                if not validate_context_budget(candidate):
                    context = candidate
            else:
                context = context_budget_for_window(window)
                context = LocalContextBudget(
                    context.context_window_tokens,
                    context.context_reserve_tokens,
                    context.context_keep_recent_tokens,
                    source="config",
                )
    return LocalProviderConfig(
        base_url=str(raw.get("base_url") or "").strip(),
        model=str(raw.get("model") or "").strip(),
        api_key=str(raw.get("api_key") or ""),
        native_tools_mode=mode,
        context=context,
    )


def load_local_config() -> LocalProviderConfig:
    """Read the canonical config file (legacy shapes migrate on read)."""
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
    if mode is None:
        legacy = _parse_bool_flag(body.get("native_tools")) if "native_tools" in body else None
        mode = (
            NATIVE_TOOLS_ON
            if legacy
            else NATIVE_TOOLS_OFF
            if legacy is False
            else previous.native_tools_mode
        )
    window = _parse_positive_int(body.get("context_window_tokens"))
    context = previous.context
    if window is not None:
        if window <= 0:
            return None, "context_window_tokens must be a positive integer"
        preset = context_budget_for_window(window)
        reserve = _parse_positive_int(body.get("context_reserve_tokens"))
        keep = _parse_positive_int(body.get("context_keep_recent_tokens"))
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
    try:
        from codey.providers.capabilities import capability_for

        return bool(capability_for("local").native_tools_default)
    except Exception:
        return True


def resolve_local_context_budget(config: LocalProviderConfig) -> LocalContextBudget:
    """Env fields win per-field; then stored context; then defaults.

    Invalid combinations fail open to the capability defaults.
    """
    defaults = _default_budget()
    window = _parse_positive_int(os.environ.get(LOCAL_OPENAI_CONTEXT_WINDOW_ENV, "").strip())
    reserve = _parse_positive_int(os.environ.get(LOCAL_OPENAI_CONTEXT_RESERVE_ENV, "").strip())
    keep = _parse_positive_int(os.environ.get(LOCAL_OPENAI_CONTEXT_KEEP_ENV, "").strip())
    stored = config.context
    candidate = LocalContextBudget(
        window if window is not None else (stored.context_window_tokens if stored else defaults.context_window_tokens),
        reserve if reserve is not None else (stored.context_reserve_tokens if stored else defaults.context_reserve_tokens),
        keep if keep is not None else (stored.context_keep_recent_tokens if stored else defaults.context_keep_recent_tokens),
        source="config" if stored else defaults.source,
    )
    if validate_context_budget(candidate):
        return defaults
    if any(v is not None for v in (window, reserve, keep)):
        return LocalContextBudget(
            candidate.context_window_tokens,
            candidate.context_reserve_tokens,
            candidate.context_keep_recent_tokens,
            source="env",
        )
    return candidate


_MISSING: object = object()


def resolve_effective_local_config(
    config: LocalProviderConfig | None = None,
    endpoint: object = _MISSING,
) -> EffectiveLocalConfig:
    """Single runtime view: agent code reads this, never raw files or env.

    The endpoint comes from the caller (facades pass their mockable
    resolver); only when omitted is discovery consulted directly.
    """
    loaded = config if config is not None else load_local_config()
    resolved = endpoint
    if resolved is _MISSING:
        try:
            from codey.providers import local_discovery as discovery

            resolved = discovery.resolve_local_endpoint(
                base_url=loaded.base_url,
                model=loaded.model,
                api_key=loaded.api_key,
            )
        except Exception:
            resolved = None
    base_url = str(getattr(resolved, "base_url", "") or "") if resolved is not None else ""
    models = tuple(getattr(resolved, "models", ()) or ()) if resolved is not None else ()
    model = loaded.model or (models[0] if models else "")
    return EffectiveLocalConfig(
        base_url=base_url or loaded.base_url,
        model=model,
        api_key=loaded.api_key,
        native_tools=resolve_local_native_tools(loaded),
        context=resolve_local_context_budget(loaded),
    )


def local_bootstrap_payload() -> dict:
    """UI status: connection, models, native mode, context, presets."""
    config = load_local_config()
    try:
        from codey.providers import local_discovery as discovery

        endpoint = discovery.resolve_local_endpoint(
            base_url=config.base_url,
            model=config.model,
            api_key=config.api_key,
        )
        discovered = (
            [probe.base_url for probe in discovery.detect_local_endpoints(api_key=config.api_key)]
            if endpoint is None
            else []
        )
    except Exception:
        endpoint = None
        discovered = []
    effective = resolve_effective_local_config(config, endpoint=endpoint)
    models: list[str] = []
    if endpoint is not None:
        models = list(endpoint.models)
        if config.model and config.model not in models:
            models = [config.model, *[m for m in models if m != config.model]]
    return {
        "connected": endpoint is not None,
        "base_url": effective.base_url,
        "model": effective.model,
        "models": models,
        "candidates": discovered,
        "has_api_key": bool(config.api_key),
        "native_tools_mode": config.native_tools_mode,
        "native_tools": effective.native_tools,
        "context": asdict(effective.context),
        "context_window_tokens": effective.context.context_window_tokens,
        "context_reserve_tokens": effective.context.context_reserve_tokens,
        "context_keep_recent_tokens": effective.context.context_keep_recent_tokens,
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
    "validate_context_budget",
]
