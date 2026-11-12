"""Owned Chromium process for real CDP lifecycle tests."""
from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def chromium_executable() -> str:
    root = Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"
    candidates = sorted(root.glob("chromium-*/chrome-win64/chrome.exe"))
    assert candidates, "Playwright Chromium is not installed"
    return str(candidates[-1])


@contextmanager
def cdp_browser(profile: Path, *, port: int = 0) -> Iterator[int]:
    process = subprocess.Popen([chromium_executable(), "--headless=new", "--no-first-run",
        "--no-default-browser-check", "--disable-background-networking", f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert process.poll() is None, "test Chromium exited before CDP was available"
            marker = profile / "DevToolsActivePort"
            if not port:
                # Chromium may still hold or be writing its startup marker.
                # Retry within the same bounded CDP readiness deadline.
                with suppress(OSError, ValueError, IndexError):
                    port = int(marker.read_text(encoding="utf-8").splitlines()[0])
            if port:
                try:
                    with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    pass
            time.sleep(0.05)
        else:
            raise AssertionError("test Chromium CDP startup timed out")
        yield port
    finally:
        process.terminate()
        process.wait(timeout=10)
