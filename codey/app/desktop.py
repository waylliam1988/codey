"""Optional native window attached to the shared service."""

from __future__ import annotations

from pathlib import Path

from codey.app.desktop_startup import DesktopStartup, StartupBridge, startup_document
from codey.launcher.startup import StartupTrace, startup_stage


def open_desktop(home: Path, *, port: int = 5173) -> None:
    trace = StartupTrace(home)
    with trace.activate(), startup_stage("desktop.window_create"):
        import webview
        startup = DesktopStartup(home, port, trace)
        window = webview.create_window("Codey", html=startup_document(), js_api=StartupBridge(startup),
                                       width=1380, height=900, text_select=True)
        if window is None:
            raise RuntimeError("Could not create the Codey desktop window")
        startup.bind(window)
        window.events.shown += startup.shown
        window.events.loaded += startup.loaded
        window.events.closed += startup.close
    webview.start(private_mode=False, storage_path=str(home / "webview"))
