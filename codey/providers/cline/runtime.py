"""One shared local-account runtime, with injectable external resources."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from codey.providers.cline.auth import Auth
from codey.providers.cline.catalog import Catalog
from codey.providers.cline.credentials import TokenStore, WindowsTokenStore
from codey.providers.cline.http import HttpClient, JsonClient
from codey.providers.connection_controls import ConnectionActionError
from codey.storage.local_store import DEFAULT_STATE_HOME, read_json_strict, write_json_atomic


class Runtime:
    def __init__(self, home: Path, *, store: TokenStore, http: JsonClient,
                 browser: Callable[[str], object] | None = None, clock: Callable[[], float] = time.time) -> None:
        self.home = home
        self.auth = Auth(store, http, home / "credentials", clock=clock, **({"browser": browser} if browser else {}))
        self.catalog = Catalog(home, http, clock=clock)
        self.gate = threading.RLock()
        self.active = 0
        self.block_path = home / "blocked.json"
        try:
            self.blocked = read_json_strict(self.block_path) is not None
        except (OSError, ValueError):
            self.blocked = True
        self.closed = False

    def block_unexpected_cost(self) -> None:
        with self.gate:
            self.blocked = True
            write_json_atomic(self.block_path, {"reason": "unexpected_user_cost"})

    def payload(self) -> dict[str, Any]:
        auth = self.auth.snapshot()
        payload = self.catalog.payload()
        state = "blocked" if self.blocked or auth["state"] == "reauth_required" else (
            "ready" if auth["state"] == "ready" and payload["models"] else "unavailable")
        return {**payload, "state": state, "authentication": auth}

    def action(self, action: str, attempt: str | None) -> dict[str, Any]:
        if action == "login":
            return self.auth.start_login()
        if action == "cancel_login":
            return self.auth.cancel_login(attempt)
        if action == "reopen_login":
            return self.auth.reopen_login(attempt)
        if action not in {"logout", "clear_data"}:
            raise ConnectionActionError("invalid_action", "Unknown Cline connection action.")
        with self.gate:
            if self.active:
                raise ConnectionActionError("connection_in_use", "Stop tasks using Cline first.", 409)
            if action == "clear_data":
                pending = []
                try:
                    self.auth.logout()
                except (OSError, ValueError, RuntimeError):
                    pending.append("system_credentials")
                try:
                    self.catalog.clear()
                    self.block_path.unlink(missing_ok=True)
                except (OSError, ValueError, RuntimeError):
                    pending.append("model_catalog_cache")
                if pending:
                    raise ConnectionActionError("cleanup_incomplete", "Some connection data still needs cleanup.",
                                                503, pending_cleanup=tuple(pending))
            else:
                self.auth.logout()
                self.block_path.unlink(missing_ok=True)
            self.blocked = False
            return self.auth.snapshot()

    def close(self) -> None:
        with self.gate:
            self.closed = True
            self.auth.close()


_RUNTIME: Runtime | None = None
_LOCK = threading.Lock()


def runtime() -> Runtime:
    global _RUNTIME
    with _LOCK:
        if _RUNTIME is None or _RUNTIME.closed:
            _RUNTIME = Runtime(DEFAULT_STATE_HOME / "connections" / "cline", store=WindowsTokenStore(), http=HttpClient())
        return _RUNTIME


def shutdown() -> None:
    with _LOCK:
        if _RUNTIME is not None:
            _RUNTIME.close()
