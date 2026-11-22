"""Optional connector controls, dispatched without integration-specific imports."""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from codey.app.model_settings import _payload_callback, models_in_use, preferences_for
from codey.providers.api_connections import connection_for
from codey.providers.connection_controls import ConnectionActionError
from codey.types.json_value import JsonObject, decode_json_object
from codey.utils.object_fields import object_fields

if TYPE_CHECKING:
    from codey.app.context import AppContext


@runtime_checkable
class _ActionCallback(Protocol):
    def __call__(self, action: str, attempt: str | None, /) -> object: ...


@runtime_checkable
class _ObserveCatalogCallback(Protocol):
    def __call__(self, source: str, payload: Mapping[str, object], /) -> None: ...

_ACTIONS = frozenset({"login", "reopen_login", "cancel_login", "logout", "clear_data"})


def authentication_response(ctx: AppContext, query: dict[str, list[str]]) -> tuple[int, JsonObject]:
    return dispatch(ctx, {"source": (query.get("source") or [""])[0]}, read_only=True)


def action_response(ctx: AppContext, body: object) -> tuple[int, JsonObject]:
    return dispatch(ctx, body)


def dispatch(ctx: AppContext, body: object, *, read_only: bool = False) -> tuple[int, JsonObject]:
    if not object_fields(body):
        return 400, {"ok": False, "code": "invalid_action", "error": "A model source is required."}
    source = body.get("source")
    if not isinstance(source, str):
        return 400, {"ok": False, "code": "invalid_action", "error": "A model source is required."}
    action = body.get("action")
    if not read_only and (not isinstance(action, str) or action not in _ACTIONS):
        return 400, {"ok": False, "code": "invalid_action", "error": "Unknown connection action."}
    attempt = body.get("attempt_id")
    if attempt is not None and (not isinstance(attempt, str) or len(attempt) > 128):
        return 400, {"ok": False, "code": "invalid_action", "error": "Invalid login attempt."}
    try:
        try:
            connector = connection_for(source)
        except (ValueError, ImportError):
            raise ConnectionActionError("source_unavailable", "Model source is unavailable.", 404) from None
        snapshot = _payload_callback(connector, "authentication_payload")
        execute: object = getattr(connector, "connection_action", None)
        if snapshot is None or (not read_only and not isinstance(execute, _ActionCallback)):
            raise ConnectionActionError("authentication_unsupported", "This source has no login controls.", 400)
        if read_only:
            authentication = decode_json_object(snapshot())
        elif action in {"logout", "clear_data"}:
            assert isinstance(execute, _ActionCallback)
            assert isinstance(action, str)
            # This is the same lifecycle lock used by final run admission.
            # Connector gates additionally cover provider factories outside the app.
            with ctx.lock:
                if any(item["provider"] == source for item in models_in_use(ctx)):
                    raise ConnectionActionError("connection_in_use", "Stop tasks using this source first.", 409)
                authentication = decode_json_object(execute(action, attempt))
                if action == "clear_data":
                    try:
                        preferences_for(ctx).clear_source(source)
                    except (OSError, ValueError, RuntimeError):
                        raise ConnectionActionError("cleanup_incomplete", "Connection preferences still need cleanup.",
                                                    503, pending_cleanup=("model_preferences",)) from None
        else:
            assert isinstance(execute, _ActionCallback)
            assert isinstance(action, str)
            authentication = decode_json_object(execute(action, attempt))
        cached = _payload_callback(connector, "cached_model_payload")
        payload: JsonObject = decode_json_object(cached()) if cached is not None else {"id": source, "models": []}
        coordinator: object = getattr(ctx, "connection_status", None)
        if coordinator is not None:
            observe: object = getattr(coordinator, "observe_catalog", None)
            if not isinstance(observe, _ObserveCatalogCallback):
                raise RuntimeError("connection status coordinator must expose observe_catalog")
            observe(source, payload)
        status = 202 if authentication.get("state") in {"starting", "awaiting_authorization", "exchanging"} else 200
        return status, {"ok": True, "authentication": authentication, "source": payload}
    except ConnectionActionError as exc:
        error: JsonObject = {"ok": False, "code": exc.code, "error": str(exc)}
        if exc.pending_cleanup:
            error["pending_cleanup"] = list(exc.pending_cleanup)
        return exc.status, error
    except (OSError, ValueError, RuntimeError):
        return 503, {"ok": False, "code": "connection_action_failed", "error": "Connection operation failed; retry."}
