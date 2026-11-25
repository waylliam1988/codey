"""Window-independent local service, discovery and explicit activation gate."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, TypeGuard

from codey.control.protocol import PROTOCOL_VERSION, Actor, ControlError
from codey.storage.file_lock import FileLease, acquire_lease
from codey.storage.local_store import read_json_strict, write_json_atomic
from codey.utils.object_fields import object_fields

if TYPE_CHECKING:
    from codey.app.context import AppContext
    from codey.app.control_service import ControlService
    from codey.app.public_event_feed import PublicEventFeed
    from codey.app.server import CodeyHTTPServer


class _Credentials(TypedDict):
    operator: str
    agent: str


def _valid_credentials(value: dict[str, object]) -> TypeGuard[_Credentials]:
    return all(isinstance(token, str) and len(token) >= 32 for token in (value.get(key) for key in ("operator", "agent")))


def credentials(home: Path) -> _Credentials:
    path = home / "control-credentials.json"
    if path.is_symlink():
        raise ValueError("Control credentials cannot be a symbolic link")
    existing = read_json_strict(path, max_bytes=8192)
    if existing is not None:
        if not _valid_credentials(existing):
            raise ValueError("Invalid local control credentials")
        return existing
    value: _Credentials = {"operator": secrets.token_urlsafe(48), "agent": secrets.token_urlsafe(48)}
    write_json_atomic(path, {"operator": value["operator"], "agent": value["agent"]}, mode=0o600, max_bytes=8192)
    return value


class LocalService:
    def __init__(self, home: Path, *, port: int = 0, allowed_roots: tuple[Path, ...] = (),
                 verify_only: bool = False, release_id: str = "") -> None:
        self.home = home.expanduser().resolve()
        self.port = port
        self.allowed_roots = allowed_roots
        self.verify_only = verify_only
        self.release_id = release_id
        self.stop_event = threading.Event()
        self.ctx: AppContext | None = None
        self.httpd: CodeyHTTPServer | None = None
        self.control: ControlService | None = None
        self.feed: PublicEventFeed | None = None
        self.lease: FileLease | None = None
        self.thread: threading.Thread | None = None
        self._unobserve: Callable[[], None] | None = None
        self._secrets: _Credentials | None = None
        self._publish_lock = threading.Lock()
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
            if not self.release_id or self.release_id == "source":
                raise ValueError("LocalService requires an immutable release_id")
            self._secrets = credentials(self.home)
            self.ctx = AppContext(self.home, start_scheduler=False)
            sibling_probe.bind_provider_handlers(self.ctx)
            self.control = ControlService(self.ctx, allowed_roots=self.allowed_roots, release_id=self.release_id)
            self.control.draining = self.verify_only
            self.control.verifying = self.verify_only
            self.instance_id = self.control.instance_id
            self.ctx.control_service = self.control
            feed = self.feed = PublicEventFeed()

            def publish_event(sequence: int, event: dict[str, object]) -> None:
                feed.publish(sequence, event)

            self._unobserve = self.ctx.event_bus.observe(publish_event)
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
            continuation = partial(task_submit.submit_task_after_slot_release, get_state=self._context)
            self.ctx.durable_scheduler.set_health_callback(self._publish)
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
        if self._secrets is None:
            raise RuntimeError("Local service credentials are not loaded")
        if secrets.compare_digest(token, self._secrets["operator"]):
            return Actor("user", "User", True)
        if secrets.compare_digest(token, self._secrets["agent"]):
            return Actor("codex", "Codex")
        return None

    def _context(self) -> AppContext:
        if self.ctx is None:
            raise RuntimeError("Local service context is not running")
        return self.ctx

    def _publish(self) -> None:
        # Startup, lifecycle requests and scheduler health callbacks share one
        # discovery file. Serialize the snapshot and replacement together.
        with self._publish_lock:
            control = self.control
            if control is None:
                raise RuntimeError("Local service control is not running")
            payload = {
                "protocol_version": PROTOCOL_VERSION, "instance_id": control.instance_id,
                "pid": os.getpid(), "url": f"http://127.0.0.1:{self.port}", "release_id": self.release_id,
                "status": control.health()["status"],
                "started_at": control.started_at,
            }
            for attempt in range(8):
                try:
                    write_json_atomic(self.home / "service.json", payload, mode=0o600)
                    break
                except PermissionError as exc:
                    # Windows discovery readers can briefly deny replacement.
                    winerror: object = getattr(exc, "winerror", None)
                    if winerror != 5 or attempt == 7:
                        raise
                    time.sleep(0.01 * (attempt + 1))

    def lifecycle(self, actor: Actor, operation: str, params: object) -> dict[str, object]:
        if not actor.operator:
            raise ControlError("FORBIDDEN", "Lifecycle operations require the user capability")
        if not object_fields(params) or set(params) - {"expected_instance_id"}:
            raise ControlError("INVALID_ARGUMENT", "Invalid lifecycle parameters")
        control, httpd = self.control, self.httpd
        ctx = self._context()
        if control is None or httpd is None:
            raise RuntimeError("Local service is not running")
        if params.get("expected_instance_id") != control.instance_id:
            raise ControlError("RUN_MISMATCH", "Service instance changed")
        if operation == "desktop_url":
            return {"url": httpd.launch_url(f"http://127.0.0.1:{self.port}/")}
        if operation == "activate":
            from codey.app.task_submit import submit_task_after_slot_release

            self.verify_only = False
            control.draining = False
            control.verifying = False
            ctx.shell_approval_workflow.start(partial(submit_task_after_slot_release, get_state=self._context))
            ctx.durable_scheduler.start()
            self._publish()
        elif operation == "drain":
            with ctx.task_runtime.spawn_line(), ctx.durable.control_line():
                control.draining = True
            self._publish()
        elif operation == "shutdown":
            if not control.draining or not self.quiescent():
                raise ControlError("REVISION_CONFLICT", "Service must drain and finish cleanup before shutdown")
            self.stop_event.set()
        elif operation != "status":
            raise ControlError("INVALID_ARGUMENT", "Unknown lifecycle operation")
        return {**control.health(), "quiescent": self.quiescent()}

    def quiescent(self) -> bool:
        ctx = self._context()
        return not ctx.task_runtime.active_runs() and not ctx.lifecycle.shell_executions and ctx.shell_approval_workflow.quiescent()

    def run(self) -> None:
        try:
            self.start()
            assert self.control is not None
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
            if self.feed is not None:
                self.feed.close()
            self.httpd.server_close()
            self.httpd = None
        if self.control is not None:
            self.control.close()
            self.control = None
        if self._unobserve is not None:
            self._unobserve()
            self._unobserve = None
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
            try:
                current = read_json_strict(self.home / "service.json")
                if current and current.get("instance_id") == self.instance_id:
                    with contextlib.suppress(FileNotFoundError):
                        (self.home / "service.json").unlink()
            finally:
                self.lease.release()
                self.lease = None
