"""One desktop window observes the existing shared-service startup path."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Protocol

from codey.control.client import ensure_service
from codey.launcher.startup import StartupTrace, startup_stage


class DesktopWindow(Protocol):
    def evaluate_js(self, script: str) -> object: ...
    def load_url(self, url: str) -> None: ...
    def load_html(self, content: str, /) -> None: ...


def startup_document() -> str:
    tokens = (Path(__file__).parents[1] / "web" / "assets" / "tokens.css").read_text(encoding="utf-8")
    return """<!doctype html><html lang="en"><meta charset="utf-8"><title>Codey</title><style>""" + tokens + """
    html,body { height:100%; margin:0; background:var(--bg); color:var(--text); font:14px/1.6 var(--font-sans); }
    body { display:grid; place-items:center; } main { max-width:480px; padding:32px; }
    h1 { font-size:16px; font-weight:500; margin:0 0 8px; } p { margin:0; color:var(--text-dim); }
    #error { margin-top:12px; white-space:pre-wrap; overflow-wrap:anywhere; }
    button { margin-top:16px; padding:5px 12px; font:inherit; color:var(--text); background:var(--panel);
      border:1px solid var(--border); border-radius:6px; cursor:pointer; }
    [hidden] { display:none; }
    </style><main><h1>Starting Codey</h1><p id="status" role="status" aria-live="polite">Preparing the workspace</p>
    <p id="error" role="alert" hidden></p><button id="retry" hidden onclick="this.disabled=true;window.pywebview.api.retry()">Retry</button></main>
    <script>window.codeyStartup=function(data){document.getElementById('status').textContent=data.status;
    const error=document.getElementById('error');error.textContent=data.error;error.hidden=!data.error;
    const retry=document.getElementById('retry');retry.hidden=!data.error;retry.disabled=false;};</script></html>"""


_LABELS = {
    "startup.lock": "Waiting for Codey", "frontend.check": "Checking the interface",
    "release.snapshot": "Preparing Codey", "service.health": "Connecting to Codey",
    "service.launch": "Starting the service", "service.activate": "Updating Codey",
    "release.prepare": "Checking the update", "validation.static": "Checking the update",
    "validation.command": "Validating Codey", "validation.database_backup": "Preparing the data check",
    "validation.cache_write": "Saving the code check",
    "validation.database_preflight": "Checking the current data", "desktop.navigate": "Opening the workspace",
}


class DesktopStartup:
    def __init__(self, home: Path, port: int, trace: StartupTrace) -> None:
        self._home, self._port, self._trace = home, port, trace
        self._window: DesktopWindow | None = None
        self._gate = threading.Lock()
        self._closed = threading.Event()
        self._initial_loaded = False
        self._navigating = False
        self._failed = False

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

    def close(self) -> None:
        self._closed.set()

    def _show(self, status: str, error: str = "") -> None:
        if self._closed.is_set() or self._window is None or self._navigating:
            return
        try:
            self._window.evaluate_js("window.codeyStartup(" + json.dumps({"status": status, "error": error}) + ")")
        except Exception:
            self._trace.record("desktop.status_unavailable", outcome="failed")

    def _progress(self, phase: str) -> None:
        self._show(_LABELS.get(phase, "Starting Codey"))

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
        self._trace.progress = self._progress
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
                    self._navigating = True
                    if self._window is not None and not self._closed.is_set():
                        self._window.load_url(url)
        except Exception as exc:
            self._fail(exc)
        finally:
            self._gate.release()

    def retry(self) -> None:
        self._schedule(retry=True)

    def _fail(self, error: Exception) -> None:
        self._navigating = False
        self._failed = True
        self._trace.record("desktop.startup_failed", outcome="failed")
        self._show("Could not start Codey", str(error)[:2000] or "Check startup-timings.jsonl and try again.")

    def _schedule(self, *, retry: bool) -> None:
        if not self._begin(retry=retry):
            return
        try:
            threading.Thread(target=self._run, name="codey-desktop-startup", daemon=True).start()
        except Exception as exc:
            self._fail(exc)
            self._gate.release()

    def ui_ready(self, startup_id: str) -> None:
        if self._navigating and startup_id == self._trace.identity and not self._closed.is_set():
            self._trace.record("desktop.interface_ready")
            self._navigating = False

    def ui_failed(self, startup_id: str, message: str) -> None:
        if not self._navigating or startup_id != self._trace.identity or self._closed.is_set():
            return
        self._navigating = False
        self._failed = True
        self._trace.record("desktop.interface_failed", outcome="failed")
        if self._window is not None:
            try:
                self._window.load_html(startup_document())
                self._show("Could not open the workspace", message[:2000])
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
