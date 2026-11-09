"""Optional native window attached to the shared service."""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from codey.app.desktop_startup import DesktopStartup, StartupBridge, startup_document
from codey.launcher.startup import StartupTrace, startup_stage


def _set_taskbar_identity() -> None:
    if sys.platform != "win32":
        return
    set_identity = ctypes.OleDLL("shell32").SetCurrentProcessExplicitAppUserModelID
    set_identity.argtypes = [ctypes.c_wchar_p]
    set_identity("Codey.Desktop")


def open_desktop(home: Path, *, port: int = 5173) -> None:
    trace = StartupTrace(home)
    with trace.activate(), startup_stage("desktop.window_create"):
        _set_taskbar_identity()
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
    icon = Path(__file__).resolve().parents[1] / "web" / "icon.ico"
    webview.start(func=startup.start, private_mode=False, storage_path=str(home / "webview"), icon=str(icon))
