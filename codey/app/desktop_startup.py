"""One desktop window observes the existing shared-service startup path."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Protocol

from codey.control.client import ensure_service
from codey.launcher.startup import StartupTrace, startup_stage

INTERFACE_READY_SECONDS = 30

class DesktopWindow(Protocol):
    def load_url(self, url: str) -> None: ...
    def load_html(self, content: str, /) -> None: ...


def startup_document(error: str = "") -> str:
    assets = Path(__file__).parents[1] / "web" / "assets"
    styles = "\n".join((assets / name).read_text(encoding="utf-8")
                       for name in ("tokens.css", "desktop-startup.css"))
    script = (assets / "desktop-startup.js").read_text(encoding="utf-8")
    feedback = "window.codeyStartup(" + json.dumps({"error": error}).replace("<", "\\u003c") + ");"
    return ("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>Codey</title><style>"
            + styles + "html,body{height:100%;margin:0;background:var(--bg)}"
            + "</style><body data-desktop-startup><script>" + script.replace("</script", "<\\/script")
            + feedback.replace("</script", "<\\/script") + "</script></body></html>")


class DesktopStartup:
    def __init__(self, home: Path, port: int, trace: StartupTrace) -> None:
        self._home, self._port, self._trace = home, port, trace
        self._window: DesktopWindow | None = None
        self._gate = threading.Lock()
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._shown = False
        self._started = False
        self._navigating = False
        self._failed = False
        self._failure_message = ""
        self._url: str | None = None
        self._ready_timer: threading.Timer | None = None
        self._deadline: object | None = None

    def bind(self, window: DesktopWindow) -> None:
        self._window = window

    def shown(self) -> None:
        with self._lock:
            if self._shown or self._closed.is_set():
                return
            self._shown = True
            self._trace.record("desktop.window_shown")
            self._cancel_deadline()
        self._deliver()

    def loaded(self) -> None:
        # Observation only. Service preparation never waits for document/JS readiness.
        self._trace.record("desktop.document_loaded" if self._navigating else "desktop.startup_document_loaded")

    def close(self) -> None:
        with self._lock:
            self._closed.set()
            self._navigating = False
            self._cancel_deadline()

    def _cancel_deadline(self) -> None:
        if self._ready_timer is not None:
            self._ready_timer.cancel()
            self._ready_timer = None
        self._deadline = None

    def _arm_deadline(self, message: str) -> None:
        self._cancel_deadline()
        token = self._deadline = object()
        self._ready_timer = threading.Timer(
            INTERFACE_READY_SECONDS, self._expire, args=(self._trace.identity, token, message),
        )
        self._ready_timer.daemon = True
        self._ready_timer.start()

    def _expire(self, startup_id: str, token: object, message: str) -> None:
        with self._lock:
            if token is not self._deadline:
                return
            self._fail_locked(startup_id, message)
        self._deliver()

    def _fail_locked(self, startup_id: str, message: str) -> None:
        if startup_id != self._trace.identity or self._closed.is_set():
            return
        self._cancel_deadline()
        self._navigating = False
        self._url = None
        self._failed = True
        self._failure_message = message[:2000]
        self._trace.record("desktop.startup_failed", outcome="failed")

    def _deliver(self) -> None:
        url: str | None = None
        trace = self._trace
        try:
            with self._lock:
                trace = self._trace
                if self._closed.is_set() or not self._shown or self._window is None:
                    return
                window = self._window
                if self._failed:
                    content = startup_document(self._failure_message)
                elif self._url and not self._navigating:
                    content, url = None, self._url
                    self._url = None
                    self._navigating = True
                    self._arm_deadline("The interface did not become ready. Retry to reload it.")
                else:
                    return
            # Native navigation needs a shown host, not a working JS bridge.
            # Presentation never holds the service preparation gate.
            if url is not None:
                with trace.activate(), startup_stage("desktop.navigate"):
                    window.load_url(url)
            elif content is not None:
                window.load_html(content)
        except Exception as exc:
            with self._lock:
                trace.record("desktop.navigation_failed", outcome="failed")
                if trace is not self._trace or self._closed.is_set():
                    return
                self._fail_locked(trace.identity, str(exc) or "Could not open the interface. Retry to reload it.")
            if url is not None:
                self._deliver()

    def _begin(self, *, retry: bool) -> bool:
        with self._lock:
            if self._closed.is_set() or (not retry and self._started) or (retry and not self._failed):
                return False
            if not self._gate.acquire(blocking=False):
                return False
            self._started = True
            if retry:
                self._trace = StartupTrace(self._home)
            self._failed = False
            self._failure_message = ""
            self._url = None
            self._navigating = False
            if not self._shown:
                try:
                    self._arm_deadline("The desktop window did not become ready. Retry to reload it.")
                except Exception as exc:
                    self._gate.release()
                    self._fail_locked(self._trace.identity, str(exc))
                    return False
            return True

    def start(self) -> None:
        if self._begin(retry=False):
            self._run()

    def _run(self) -> None:
        trace = self._trace
        error: Exception | None = None
        url: str | None = None
        try:
            if self._closed.is_set():
                return
            with trace.activate(), startup_stage("desktop.service_ready"):
                client = ensure_service(self._home, port=self._port)
                if self._closed.is_set():
                    return
                with startup_stage("desktop.url"):
                    url = str(client.lifecycle("desktop_url")["url"])
                    url += ("&" if "#" in url else "#") + "codey_startup=" + trace.identity
        except Exception as exc:
            error = exc
        finally:
            self._gate.release()
        with self._lock:
            if error is not None:
                self._fail_locked(trace.identity, str(error) or "Could not start Codey. Try again.")
            elif trace is self._trace and not self._failed:
                self._url = url
        self._deliver()

    def retry(self) -> bool:
        if not self._begin(retry=True):
            return False
        try:
            threading.Thread(target=self._run, name="codey-desktop-startup", daemon=True).start()
        except Exception as exc:
            self._gate.release()
            with self._lock:
                self._fail_locked(self._trace.identity, str(exc))
            self._deliver()
        return True

    def ui_ready(self, startup_id: str) -> None:
        with self._lock:
            if not self._navigating or startup_id != self._trace.identity or self._closed.is_set():
                return
            self._cancel_deadline()
            self._navigating = False
            self._trace.record("desktop.interface_ready")

    def ui_failed(self, startup_id: str, message: str) -> None:
        with self._lock:
            if not self._navigating or startup_id != self._trace.identity or self._closed.is_set():
                return
            self._fail_locked(startup_id, message)
        self._deliver()


class StartupBridge:
    def __init__(self, startup: DesktopStartup) -> None:
        self._startup = startup

    def retry(self) -> bool:
        return self._startup.retry()

    def ui_ready(self, startup_id: str) -> None:
        self._startup.ui_ready(startup_id)

    def ui_failed(self, startup_id: str, message: str) -> None:
        self._startup.ui_failed(startup_id, message)
