"""Lazy connection factories. The generation runtime imports no connector."""
from __future__ import annotations

import importlib
from types import ModuleType

from codey.providers.base import ChatProvider
from codey.providers.catalog import API_CONNECTIONS
from codey.runtime.core.api_selection import ApiRunSelection


def connection_for(connection_id: str) -> ModuleType:
    registration = API_CONNECTIONS.get(connection_id)
    if registration is None:
        raise ValueError("original API connection is unavailable")
    return importlib.import_module(registration[1])


def capture_selection(connection_id: str, selection: object = None) -> ApiRunSelection:
    return connection_for(connection_id).capture_selection(selection)  # type: ignore[no-any-return]


def open_selection(selection: ApiRunSelection) -> ChatProvider:
    return connection_for(selection.connection_id).open_selection(selection)  # type: ignore[no-any-return]


def capture_reviewer_selection(writer: ApiRunSelection) -> ApiRunSelection:
    """Admit a different model from the same connector's current directory."""
    connection = connection_for(writer.connection_id)
    models = connection.model_payload().get("models", [])
    for model in sorted(models, key=lambda item: item.get("review_eligible") is not True):
        if model["id"] != writer.model_id and model.get("review_eligible") is not False:
            return capture_selection(writer.connection_id, {"model": model["id"]})
    raise ValueError("no independent API reviewer model is currently available")
