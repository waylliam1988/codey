"""One model settings surface; optional connectors contribute catalog data."""

from __future__ import annotations

from typing import Any, cast

from codey.providers.catalog import API_CONNECTIONS, WEB_PROVIDER_LABELS
from codey.providers.model_preferences import ModelPreferences, ModelPreferencesConflict, allows, normalize_sources


def preferences_for(ctx: Any = None) -> ModelPreferences:
    return cast(ModelPreferences, ctx.providers.model_preferences) if ctx is not None else ModelPreferences()


def models_in_use(ctx: Any) -> list[dict[str, str]]:
    active = ctx.current_run()
    used = (
        [
            {
                "provider": active.provider_id,
                "model": getattr(ctx.run_registry.api_selection_for(active.run_id), "model_id", ""),
            }
        ]
        if active
        else []
    )
    used.extend(
        {
            "provider": item.get("provider") or item.get("provider_id") or "",
            "model": getattr(item.get("_api_selection"), "model_id", ""),
        }
        for item in ctx.approvals.shell_snapshot().values()
    )
    used.extend({"provider": provider, "model": model} for provider, model in ctx.providers.model_uses)
    return used


def settings_response(ctx: Any) -> tuple[int, dict[str, Any]]:
    store = preferences_for(ctx)
    sources: list[dict[str, Any]] = [
        {
            "id": "websites",
            "label": "Websites",
            "models": [{"id": key, "name": name} for key, name in WEB_PROVIDER_LABELS.items()],
        }
    ]
    for key, (label, _) in API_CONNECTIONS.items():
        sources.append(
            {**store.catalog(key), "id": key, "label": label, "discoverable": True, "connection_editor": key == "local"}
        )
    with ctx.lock:
        in_use = models_in_use(ctx)
    return 200, {"ok": True, "preferences": store.snapshot(), "sources": sources, "in_use": in_use}


def save_response(ctx: Any, body: object) -> tuple[int, dict[str, Any]]:
    if not isinstance(body, dict) or type(body.get("base_revision")) is not int:
        return 400, {"ok": False, "error": "base_revision required"}
    try:
        sources = normalize_sources(body.get("sources"))
        with ctx.lock:
            if any(
                item["provider"] and not allows(sources, item["provider"], item["model"]) for item in models_in_use(ctx)
            ):
                return 409, {
                    "ok": False,
                    "code": "model_in_use",
                    "error": "This model is in use. Stop the task before disabling it.",
                }
            preferences_for(ctx).save(sources, base_revision=body["base_revision"])
    except ModelPreferencesConflict as exc:
        return 409, {"ok": False, "code": "model_settings_conflict", "error": str(exc)}
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except OSError as exc:
        return 500, {"ok": False, "error": str(exc)}
    return settings_response(ctx)


def discover_response(ctx: Any, body: object) -> tuple[int, dict[str, Any]]:
    source = body.get("source") if isinstance(body, dict) else None
    if source not in API_CONNECTIONS:
        return 400, {"ok": False, "error": "model source unavailable"}
    from codey.providers.api_connections import connection_for

    try:
        payload = connection_for(source).model_payload()
        preferences_for(ctx).observe_catalog(payload)
    except (OSError, ValueError, RuntimeError) as exc:
        return 400, {"ok": False, "error": str(exc)}
    return 200, {"ok": True, "source": payload}
