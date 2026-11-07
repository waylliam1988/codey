"""Lazy connection factories. The generation runtime imports no connector."""
from __future__ import annotations

import importlib
from types import ModuleType

from codey.providers.base import ChatProvider
from codey.providers.catalog import API_CONNECTIONS
from codey.providers.compat_transport import optional_callable
from codey.runtime.core.api_selection import ApiRunSelection
from codey.types.json_value import decode_json_object


def connection_for(connection_id: str) -> ModuleType:
    registration = API_CONNECTIONS.get(connection_id)
    if registration is None:
        raise ValueError("original API connection is unavailable")
    return importlib.import_module(registration[1])


def cached_model_supports_images(connection_id: str, model_id: str) -> bool:
    """Read an optional connector's cached capability through its registration."""
    if connection_id not in API_CONNECTIONS:
        return False
    cached = optional_callable(connection_for(connection_id), "cached_model_payload")
    if cached is None:
        return False
    models = decode_json_object(cached()).get("models", [])
    if not isinstance(models, list):
        raise ValueError("cached models must be an array")
    return any(isinstance(row, dict) and row.get("id") == model_id and row.get("supports_images") is True
               for row in models)


def capture_selection(connection_id: str, selection: object = None) -> ApiRunSelection:
    return connection_for(connection_id).capture_selection(selection)  # type: ignore[no-any-return]


def open_selection(selection: ApiRunSelection) -> ChatProvider:
    return connection_for(selection.connection_id).open_selection(selection)  # type: ignore[no-any-return]


def capture_reviewer_selection(writer: ApiRunSelection, *, allowed_models: set[str] | None = None) -> ApiRunSelection:
    """Admit a different model from the same connector's current directory."""
    connection = connection_for(writer.connection_id)
    models = connection.model_payload().get("models", [])
    for model in sorted(models, key=lambda item: item.get("review_eligible") is not True):
        if allowed_models is not None and model["id"] not in allowed_models:
            continue
        if model["id"] != writer.model_id and model.get("review_eligible") is not False:
            return capture_selection(writer.connection_id, {"model": model["id"]})
    raise ValueError("no independent API reviewer model is currently available")


def capture_cross_connection_reviewer(
    *,
    excluded_connections: set[str] | None = None,
    allowed_models_by_connection: dict[str, set[str]] | None = None,
) -> tuple[str, ApiRunSelection]:
    """Select one enabled model from another API connection for Review.

    The caller supplies the user's enabled model scope. Discovery is still
    connector-owned, so this helper never invents an endpoint or credentials.
    """
    excluded = excluded_connections or set()
    allowed = allowed_models_by_connection or {}
    for connection_id in API_CONNECTIONS:
        if connection_id in excluded:
            continue
        selected = allowed.get(connection_id, set())
        if not selected:
            continue
        try:
            connection = connection_for(connection_id)
            models = connection.model_payload().get("models", [])
        except (ImportError, OSError, RuntimeError, ValueError):
            # Discovery is a fallback queue: a broken connector must not
            # prevent trying the next explicitly enabled API source.
            continue
        for model in models:
            if not isinstance(model, dict):
                continue
            model_id = model.get("id")
            if not isinstance(model_id, str) or model_id not in selected:
                continue
            if model.get("review_eligible") is False:
                continue
            return connection_id, connection.capture_selection({"model": model_id})
    raise ValueError("no independent API reviewer model is currently available")
