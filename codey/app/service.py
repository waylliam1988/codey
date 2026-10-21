"""Window-independent local service, discovery and explicit activation gate."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
import threading
import time
from functools import partial
from pathlib import Path
from typing import Any

from codey.control.protocol import PROTOCOL_VERSION, Actor, ControlError
from codey.storage.file_lock import acquire_lease
from codey.storage.local_store import read_json_strict, write_json_atomic


def credentials(home: Path) -> dict[str, Any]:
    path = home / "control-credentials.json"
    if path.is_symlink():
        raise ValueError("Control credentials cannot be a symbolic link")
    existing = read_json_strict(path, max_bytes=8192)
    if existing is not None:
        if not all(isinstance(existing.get(key), str) and len(existing[key]) >= 32 for key in ("operator", "agent")):
            raise ValueError("Invalid local control credentials")
        return existing
    value = {"operator": secrets.token_urlsafe(48), "agent": secrets.token_urlsafe(48)}
    write_json_atomic(path, value, mode=0o600, max_bytes=8192)
    return value


class LocalService:
    def __init__(self, home: Path, *, port: int = 0, allowed_roots: tuple[Path, ...] = (),
                 verify_only: bool = False, release_id: str = "source") -> None:
        self.home = home.expanduser().resolve()
        self.port = port
        self.allowed_roots = allowed_roots
        self.verify_only = verify_only
        self.release_id = release_id
        self.stop_event = threading.Event()
        self.ctx: Any = None
        self.httpd: Any = None
        self.control: Any = None
        self.feed: Any = None
        self.lease: Any = None
        self.thread: threading.Thread | None = None
        self._unobserve: Any = None
        self._secrets: dict[str, Any] = {}
        self.instance_id = ""

    def start(self) -> LocalService:
        from codey.app import sibling_probe, task_submit
        from codey.app.context import AppContext
        from codey.app.control_service import ControlService
        from codey.app.operator_auth import OperatorAuth
        from codey.app.public_event_feed import PublicEventFeed
        from codey.app.server import CodeyHTTPServer, Handler

        self.home.mkdir(parents=True, exist_ok=True)
        self.lease = acquire_lease(self.home / ".server.lock", timeout_seconds=0.0)
        try:
            if self.release_id == "source":
                from codey.launcher.releases import bootstrap

                self.release_id = bootstrap(self.home)["release_id"]
            self._secrets = credentials(self.home)
            self.ctx = AppContext(self.home, start_scheduler=False)
            sibling_probe.bind_provider_handlers(self.ctx)
            self.control = ControlService(self.ctx, allowed_roots=self.allowed_roots, release_id=self.release_id)
            self.control.draining = self.verify_only
            self.control.verifying = self.verify_only
            self.instance_id = self.control.instance_id
            self.ctx.control_service = self.control
            self.feed = PublicEventFeed()
            self._unobserve = self.ctx.event_bus.observe(self.feed.publish)
            self.httpd = CodeyHTTPServer(("127.0.0.1", self.port), Handler)
            self.httpd.app_context = self.ctx
            self.httpd.control_service = self.control
            self.httpd.public_feed = self.feed
            self.httpd.control_auth = self.authenticate
            self.httpd.lifecycle = self.lifecycle
            self.port = self.httpd.server_address[1]
            # A replacement service shares the browser session, while launch
            # links remain single-use and the API operator capability stays private.
            browser_token = hmac.new(self._secrets["operator"].encode(), b"codey-browser-session-v1", hashlib.sha256).hexdigest()
            self.httpd.operator_auth = OperatorAuth(self.port, session_token=browser_token)
            continuation = partial(task_submit.submit_task_after_slot_release, get_state=lambda: self.ctx)
            if not self.verify_only:
                self.ctx.shell_approval_workflow.start(continuation)
                self.ctx.durable_scheduler.start()
            self._publish()
            self.thread = threading.Thread(target=self.httpd.serve_forever, name="codey-http", daemon=True)
            self.thread.start()
            return self
        except BaseException:
            self.close()
            raise

    def authenticate(self, header: str) -> Actor | None:
        if not header.startswith("Bearer "):
            return None
        token = header[7:]
        if not token.isascii():
            return None
        if secrets.compare_digest(token, self._secrets["operator"]):
            return Actor("user", "User", True)
        if secrets.compare_digest(token, self._secrets["agent"]):
            return Actor("codex", "Codex")
        return None

    def _publish(self) -> None:
        write_json_atomic(self.home / "service.json", {
            "protocol_version": PROTOCOL_VERSION, "instance_id": self.control.instance_id,
            "pid": os.getpid(), "url": f"http://127.0.0.1:{self.port}", "release_id": self.release_id,
            "status": "verifying" if self.verify_only else "draining" if self.control.draining else "ready",
            "started_at": self.control.started_at,
        }, mode=0o600)

    def lifecycle(self, actor: Actor, operation: str, params: object) -> dict[str, Any]:
        if not actor.operator:
            raise ControlError("FORBIDDEN", "Lifecycle operations require the user capability")
        if not isinstance(params, dict) or set(params) - {"expected_instance_id"}:
            raise ControlError("INVALID_ARGUMENT", "Invalid lifecycle parameters")
        if params.get("expected_instance_id") != self.control.instance_id:
            raise ControlError("RUN_MISMATCH", "Service instance changed")
        if operation == "desktop_url":
            return {"url": self.httpd.launch_url(f"http://127.0.0.1:{self.port}/")}
        if operation == "activate":
            from codey.app.task_submit import submit_task_after_slot_release

            self.verify_only = False
            self.control.draining = False
            self.control.verifying = False
            self.ctx.shell_approval_workflow.start(partial(submit_task_after_slot_release, get_state=lambda: self.ctx))
            self.ctx.durable_scheduler.start()
            self._publish()
        elif operation == "drain":
            with self.ctx._shell_spawn_gate, self.ctx.durable.control_line():
                self.control.draining = True
            self._publish()
        elif operation == "shutdown":
            if not self.control.draining or not self.quiescent():
                raise ControlError("REVISION_CONFLICT", "Service must drain and finish cleanup before shutdown")
            self.stop_event.set()
        elif operation != "status":
            raise ControlError("INVALID_ARGUMENT", "Unknown lifecycle operation")
        return {**self.control.health(), "quiescent": self.quiescent()}

    def quiescent(self) -> bool:
        return not self.ctx.run_registry.active_runs() and not self.ctx._shell_executions and self.ctx.shell_approval_workflow.quiescent()

    def run(self) -> None:
        try:
            self.start()
            while not self.stop_event.wait(0.25):
                self.control.poll()
        except KeyboardInterrupt:
            self.stop_event.set()
        finally:
            self.close()

    def close(self) -> None:
        if self.httpd is not None:
            if self.thread is not None and self.thread.is_alive():
                self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.control is not None:
            self.control.close()
            self.control = None
        if self._unobserve is not None:
            self._unobserve()
            self._unobserve = None
        if self.feed is not None:
            self.feed.close()
        if self.ctx is not None:
            for _attempt in range(30):
                if self.ctx.close():
                    self.ctx = None
                    break
                time.sleep(0.2)
            if self.ctx is not None:
                raise RuntimeError("Service cleanup remains active; ownership was not released")
        if self.lease is not None:
            # A successor may have replaced discovery after this process was
            # stopped; remove only the registration owned by this instance.
            current = read_json_strict(self.home / "service.json")
            if current and current.get("instance_id") == self.instance_id:
                with contextlib.suppress(FileNotFoundError):
                    (self.home / "service.json").unlink()
            self.lease.release()
            self.lease = None
