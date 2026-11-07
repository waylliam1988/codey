"""One model settings surface; optional connectors contribute catalog data."""

from __future__ import annotations

from typing import TYPE_CHECKING

from codey.providers.catalog import API_CONNECTIONS, WEB_PROVIDER_LABELS
from codey.providers.compat_transport import optional_callable, require_callable
from codey.providers.model_preferences import ModelPreferences, ModelPreferencesConflict, allows, normalize_sources
from codey.runtime.core.api_selection import ApiRunSelection
from codey.types.json_value import JsonObject, decode_json_object

if TYPE_CHECKING:
    from codey.app.context import AppContext


def preferences_for(ctx: AppContext | None = None) -> ModelPreferences:
    return ctx.providers.model_preferences if ctx is not None else ModelPreferences()


def models_in_use(ctx: AppContext) -> list[dict[str, str]]:
    used: list[dict[str, str]] = []
    for active in ctx.active_runs():
        selection = ctx.run_registry.api_selection_for(active.run_id)
        used.append({"provider": active.provider_id, "model": selection.model_id if selection is not None else ""})
    approvals: dict[str, dict[str, object]] = ctx.approvals.shell_snapshot()
    for item in approvals.values():
        provider = item.get("provider") or item.get("provider_id") or ""
        selection_raw = item.get("_api_selection")
        used.append({
            "provider": provider if isinstance(provider, str) else "",
            "model": selection_raw.model_id if isinstance(selection_raw, ApiRunSelection) else "",
        })
    used.extend({"provider": provider, "model": model} for provider, model in ctx.providers.model_uses_snapshot())
    return used


def settings_response(ctx: AppContext) -> tuple[int, JsonObject]:
    from codey.providers.api_connections import connection_for

    store = preferences_for(ctx)
    sources: list[JsonObject] = [
        {
            "id": "websites",
            "label": "Websites",
            "models": [{"id": key, "name": name} for key, name in WEB_PROVIDER_LABELS.items()],
        }
    ]
    for key, (label, _) in API_CONNECTIONS.items():
        connector = connection_for(key)
        cached_payload = optional_callable(connector, "cached_model_payload")
        facts = decode_json_object(cached_payload() if cached_payload is not None else store.catalog(key))
        # Catalog transport labels (for example Zen's "public") are not the
        # interactive authentication snapshot consumed by the settings panel.
        facts.pop("authentication", None)
        authentication = optional_callable(connector, "authentication_payload")
        if authentication is not None:
            facts["authentication"] = decode_json_object(authentication())
        sources.append(
            {**facts, "id": key, "label": label, "discoverable": True, "connection_editor": key == "local"}
        )
    # This is a read-only settings snapshot.  Do not hold the application
    # lifecycle lock while collecting it: a running model or shell task may
    # legitimately keep that lock for its admission/settlement transaction,
    # and opening Settings must remain responsive during that work.
    in_use = models_in_use(ctx)
    return 200, decode_json_object({"ok": True, "preferences": store.snapshot(), "sources": sources, "in_use": in_use})


def save_response(ctx: AppContext, body: object) -> tuple[int, JsonObject]:
    request: dict[str, object] = body if isinstance(body, dict) else {}
    base_revision = request.get("base_revision")
    if type(base_revision) is not int:
        return 400, {"ok": False, "error": "base_revision required"}
    try:
        sources = normalize_sources(request.get("sources"))
        with ctx.lock:
            if any(
                item["provider"] and not allows(sources, item["provider"], item["model"]) for item in models_in_use(ctx)
            ):
                return 409, {
                    "ok": False,
                    "code": "model_in_use",
                    "error": "This model is in use. Stop the task before disabling it.",
                }
            preferences_for(ctx).save(sources, base_revision=base_revision)
    except ModelPreferencesConflict as exc:
        return 409, {"ok": False, "code": "model_settings_conflict", "error": str(exc)}
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except OSError as exc:
        return 500, {"ok": False, "error": str(exc)}
    return settings_response(ctx)


def discover_response(ctx: AppContext, body: object) -> tuple[int, JsonObject]:
    request: dict[str, object] = body if isinstance(body, dict) else {}
    source = request.get("source")
    if not isinstance(source, str) or source not in API_CONNECTIONS:
        return 400, {"ok": False, "error": "model source unavailable"}
    force = request.get("force", True)
    if type(force) is not bool:
        return 400, {"ok": False, "error": "force must be true or false"}
    from codey.providers.api_connections import connection_for

    try:
        connector = connection_for(source)
        store = preferences_for(ctx)
        if not force and not store.enabled(source):
            cached = optional_callable(connector, "cached_model_payload")
            payload = decode_json_object(cached() if cached is not None else store.catalog(source))
            return 200, {"ok": True, "source": payload}
        refresh = optional_callable(connector, "refresh_models")
        payload = decode_json_object(refresh(force=force) if refresh is not None else require_callable(connector, "model_payload")())
        store.observe_catalog(payload)
        ctx.connection_status.observe_catalog(source, payload)
    except (OSError, ValueError, RuntimeError) as exc:
        return 400, {"ok": False, "error": str(exc)}
    return 200, {"ok": True, "source": payload}
