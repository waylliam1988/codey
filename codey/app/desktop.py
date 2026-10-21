"""Optional native window attached to the shared service."""

from __future__ import annotations

from pathlib import Path

from codey.control.client import ensure_service


def open_desktop(home: Path, *, port: int = 5173) -> None:
    client = ensure_service(home, port=port)
    url = client.lifecycle("desktop_url")["url"]
    import webview

    webview.create_window("Codey", url, width=1380, height=900, text_select=True)
    webview.start(private_mode=False, storage_path=str(home / "webview"))
