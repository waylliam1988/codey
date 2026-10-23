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
    def evaluate_js(self, script: str) -> object: ...
    def load_url(self, url: str) -> None: ...
    def load_html(self, content: str, /) -> None: ...


def startup_document() -> str:
    assets = Path(__file__).parents[1] / "web" / "assets"
    styles = "\n".join((assets / name).read_text(encoding="utf-8")
                       for name in ("tokens.css", "desktop-startup.css"))
    script = (assets / "desktop-startup.js").read_text(encoding="utf-8")
    return ("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>Codey</title><style>"
            + styles + "html,body{height:100%;margin:0;background:var(--bg)}"
            + "</style><body data-desktop-startup><script>" + script.replace("</script", "<\\/script")
            + "</script></body></html>")


class DesktopStartup:
    def __init__(self, home: Path, port: int, trace: StartupTrace) -> None:
        self._home, self._port, self._trace = home, port, trace
        self._window: DesktopWindow | None = None
        self._gate = threading.Lock()
        self._closed = threading.Event()
        self._initial_loaded = False
        self._navigating = False
        self._failed = False
        self._failure_message = ""
        self._navigation_lock = threading.Lock()
        self._ready_timer: threading.Timer | None = None

    def bind(self, window: DesktopWindow) -> None:
        self._window = window

    def shown(self) -> None:
        self._trace.record("desktop.window_shown")

    def loaded(self) -> None:
        if not self._initial_loaded:
            self._initial_loaded = True
            self._trace.record("desktop.startup_document_loaded")
            self._schedule(retry=False)
        elif self._navigating:
            self._trace.record("desktop.document_loaded")
        elif self._failed:
            # load_html may finish after the bridge's first error update.
            self._show(self._failure_message)

    def close(self) -> None:
        self._closed.set()
        self._finish_navigation()

    def _start_navigation(self) -> None:
        with self._navigation_lock:
            if self._closed.is_set():
                return
            self._navigating = True
            self._ready_timer = threading.Timer(
                INTERFACE_READY_SECONDS, self.ui_failed,
                args=(self._trace.identity, "The interface did not become ready. Retry to reload it."),
            )
            self._ready_timer.daemon = True
            self._ready_timer.start()

    def _finish_navigation(self, startup_id: str | None = None) -> bool:
        with self._navigation_lock:
            if startup_id is not None and (
                not self._navigating or startup_id != self._trace.identity or self._closed.is_set()
            ):
                return False
            self._navigating = False
            if self._ready_timer is not None:
                self._ready_timer.cancel()
                self._ready_timer = None
            return True

    def _show(self, error: str) -> None:
        self._failure_message = error
        if self._closed.is_set() or self._window is None or self._navigating:
            return
        try:
            self._window.evaluate_js("window.codeyStartup(" + json.dumps({"error": error}) + ")")
        except Exception:
            self._trace.record("desktop.status_unavailable", outcome="failed")

    def start(self) -> None:
        if not self._begin(retry=False):
            return
        self._run()

    def _begin(self, *, retry: bool) -> bool:
        if self._closed.is_set() or not self._gate.acquire(blocking=False):
            return False
        if retry and not self._failed:
            self._gate.release()
            return False
        if retry:
            self._trace = StartupTrace(self._home)
        self._failed = False
        self._show("")
        return True

    def _run(self) -> None:
        try:
            if self._closed.is_set():
                return
            with self._trace.activate(), startup_stage("desktop.service_ready"):
                client = ensure_service(self._home, port=self._port)
                if self._closed.is_set():
                    return
                with startup_stage("desktop.url"):
                    url = str(client.lifecycle("desktop_url")["url"])
                    url += ("&" if "#" in url else "#") + "codey_startup=" + self._trace.identity
                with startup_stage("desktop.navigate"):
                    self._start_navigation()
                    if self._window is not None and not self._closed.is_set():
                        self._window.load_url(url)
        except Exception as exc:
            self._fail(exc)
        finally:
            self._gate.release()

    def retry(self) -> None:
        self._schedule(retry=True)

    def _fail(self, error: Exception) -> None:
        self._finish_navigation()
        self._failed = True
        self._trace.record("desktop.startup_failed", outcome="failed")
        self._show(str(error)[:2000] or "Could not start Codey. Try again.")

    def _schedule(self, *, retry: bool) -> None:
        if not self._begin(retry=retry):
            return
        try:
            threading.Thread(target=self._run, name="codey-desktop-startup", daemon=True).start()
        except Exception as exc:
            self._fail(exc)
            self._gate.release()

    def ui_ready(self, startup_id: str) -> None:
        if self._finish_navigation(startup_id):
            self._trace.record("desktop.interface_ready")

    def ui_failed(self, startup_id: str, message: str) -> None:
        if not self._finish_navigation(startup_id):
            return
        self._failed = True
        self._trace.record("desktop.interface_failed", outcome="failed")
        if self._window is not None:
            try:
                self._window.load_html(startup_document())
                self._show(message[:2000])
            except Exception:
                self._trace.record("desktop.status_unavailable", outcome="failed")


class StartupBridge:
    def __init__(self, startup: DesktopStartup) -> None:
        self._startup = startup

    def retry(self) -> None:
        self._startup.retry()

    def ui_ready(self, startup_id: str) -> None:
        self._startup.ui_ready(startup_id)

    def ui_failed(self, startup_id: str, message: str) -> None:
        self._startup.ui_failed(startup_id, message)
