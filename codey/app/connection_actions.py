"""Optional connector controls, dispatched without integration-specific imports."""
from __future__ import annotations

from typing import Any

from codey.app.model_settings import models_in_use, preferences_for
from codey.providers.api_connections import connection_for
from codey.providers.connection_controls import ConnectionActionError

_ACTIONS = frozenset({"login", "reopen_login", "cancel_login", "logout", "clear_data"})


def authentication_response(ctx: Any, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    return dispatch(ctx, {"source": (query.get("source") or [""])[0]}, read_only=True)


def action_response(ctx: Any, body: object) -> tuple[int, dict[str, Any]]:
    return dispatch(ctx, body)


def dispatch(ctx: Any, body: object, *, read_only: bool = False) -> tuple[int, dict[str, Any]]:
    if not isinstance(body, dict) or not isinstance(body.get("source"), str):
        return 400, {"ok": False, "code": "invalid_action", "error": "A model source is required."}
    source = body["source"]
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
        snapshot = getattr(connector, "authentication_payload", None)
        execute = getattr(connector, "connection_action", None)
        if not callable(snapshot) or (not read_only and not callable(execute)):
            raise ConnectionActionError("authentication_unsupported", "This source has no login controls.", 400)
        if read_only:
            authentication = snapshot()
        elif action in {"logout", "clear_data"}:
            assert callable(execute)
            # This is the same lifecycle lock used by final run admission.
            # Connector gates additionally cover provider factories outside the app.
            with ctx.lock:
                if any(item["provider"] == source for item in models_in_use(ctx)):
                    raise ConnectionActionError("connection_in_use", "Stop tasks using this source first.", 409)
                authentication = execute(action, attempt)
                if action == "clear_data":
                    preferences_for(ctx).clear_source(source)
        else:
            assert callable(execute)
            authentication = execute(action, attempt)
        cached = getattr(connector, "cached_model_payload", None)
        payload = cached() if callable(cached) else {"id": source, "models": []}
        coordinator = getattr(ctx, "connection_status", None)
        if coordinator is not None:
            coordinator.observe_catalog(source, payload)
        status = 202 if authentication.get("state") in {"starting", "awaiting_authorization", "exchanging"} else 200
        return status, {"ok": True, "authentication": authentication, "source": payload}
    except ConnectionActionError as exc:
        return exc.status, {"ok": False, "code": exc.code, "error": str(exc)}
    except (OSError, ValueError, RuntimeError):
        return 503, {"ok": False, "code": "connection_action_failed", "error": "Connection operation failed; retry."}
