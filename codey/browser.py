"""Launch / attach to Edge with CDP and return a Playwright page on DeepSeek.

The approach (lifted in spirit, not in code, from codeywhere):
    1. Spawn msedge.exe with --remote-debugging-port and a dedicated --user-data-dir
       so the launch never collides with the user's normal Edge windows.
    2. Wait for the CDP port to open, then connect with Playwright over CDP.
    3. Find (or open) a tab on chat.deepseek.com and return its Page.

The dedicated profile lives at  ~/.codey/edge-profile  .  First time you run it
you log into DeepSeek once; cookies persist there forever.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

DEEPSEEK_URL = "https://chat.deepseek.com/"
DEFAULT_PORT = 9222
DEFAULT_PROFILE = Path.home() / ".codey" / "edge-profile"

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _find_edge() -> Path:
    for p in EDGE_PATHS:
        if Path(p).is_file():
            return Path(p)
    raise FileNotFoundError("msedge.exe not found in default locations")


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _launch_edge(port: int, profile: Path, start_url: str) -> subprocess.Popen:
    exe = _find_edge()
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        str(exe),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        start_url,
    ]
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(args, **kwargs)


def _wait_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open(port):
            return
        time.sleep(0.3)
    raise TimeoutError(f"CDP port {port} did not open within {timeout:.0f}s")


@dataclass
class Session:
    pw: Playwright
    browser: Browser
    page: Page

    def close(self) -> None:
        try:
            self.browser.close()
        finally:
            self.pw.stop()


def open_deepseek(port: int = DEFAULT_PORT, profile: Path = DEFAULT_PROFILE) -> Session:
    """Return a Playwright Session attached to a DeepSeek tab in our Edge profile."""
    if not _port_open(port):
        _launch_edge(port, profile, DEEPSEEK_URL)
        _wait_port(port)

    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")

    page: Page | None = None
    for ctx in browser.contexts:
        for p in ctx.pages:
            if "chat.deepseek.com" in (p.url or ""):
                page = p
                break
        if page:
            break

    if page is None:
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        page.goto(DEEPSEEK_URL, wait_until="domcontentloaded", timeout=60000)

    page.bring_to_front()
    return Session(pw=pw, browser=browser, page=page)
