"""Launch or attach to Edge with CDP for supported web chat providers.

The approach (lifted in spirit, not in code, from codeywhere):
    1. Spawn msedge.exe with --remote-debugging-port and a dedicated --user-data-dir
       so the launch never collides with the user's normal Edge windows.
    2. Wait for the CDP port to open, then connect with Playwright over CDP.
    3. Find (or open) a matching provider tab and return its Page.

The dedicated profile lives at  ~/.codey/edge-profile  .  First time you run it
you log into DeepSeek once; cookies persist there forever.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

DEEPSEEK_URL = "https://chat.deepseek.com/"
QWEN_URL = "https://chat.qwen.ai/"
MIMO_URL = "https://aistudio.xiaomimimo.com/#/c"
DEFAULT_PORT = 9222
DEFAULT_PROFILE = Path.home() / ".codey" / "edge-profile"
PROVIDER_URL_CONTAINS = {
    "deepseek": "chat.deepseek.com",
    "qwen": "chat.qwen.ai",
    "mimo": "aistudio.xiaomimimo.com",
}

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


def list_cdp_targets(port: int = DEFAULT_PORT, timeout: float = 1.0) -> list[dict]:
    """Read Edge CDP targets without starting Playwright or opening pages."""
    if not _port_open(port):
        return []
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def detect_open_provider_tabs(port: int = DEFAULT_PORT) -> dict[str, bool]:
    statuses = {provider_id: False for provider_id in PROVIDER_URL_CONTAINS}
    for target in list_cdp_targets(port):
        if str(target.get("type") or "") != "page":
            continue
        url = str(target.get("url") or "")
        for provider_id, marker in PROVIDER_URL_CONTAINS.items():
            if marker in url:
                statuses[provider_id] = True
    return statuses


@dataclass
class Session:
    pw: Playwright
    browser: Browser
    page: Page

    def close(self) -> None:
        self.pw.stop()


def _start_playwright_with_retry() -> Playwright:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            return sync_playwright().start()
        except AttributeError as exc:
            if not _is_playwright_startup_race(exc):
                raise
            last_error = exc
            time.sleep(0.25 * (attempt + 1))
    raise RuntimeError(
        "Playwright failed to initialize. Close stale Codey/Edge automation "
        "sessions and try again."
    ) from last_error


def _is_playwright_startup_race(exc: AttributeError) -> bool:
    message = str(exc)
    return "_playwright" in message or "'_playwright'" in message or '"_playwright"' in message


def open_chat_page(
    start_url: str,
    url_contains: str,
    *,
    port: int = DEFAULT_PORT,
    profile: Path = DEFAULT_PROFILE,
    open_if_missing: bool = True,
    bring_to_front: bool = True,
) -> Session:
    """Return a Playwright session attached to a matching provider tab."""
    if not _port_open(port):
        if not open_if_missing:
            raise RuntimeError(f"CDP port {port} is not open")
        _launch_edge(port, profile, start_url)
        _wait_port(port)

    pw = _start_playwright_with_retry()
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")

    page: Page | None = None
    for ctx in browser.contexts:
        for p in ctx.pages:
            if url_contains in (p.url or ""):
                page = p
                break
        if page:
            break

    if page is None:
        if not open_if_missing:
            pw.stop()
            raise RuntimeError(f"no existing provider tab matched {url_contains}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        page.goto(start_url, wait_until="domcontentloaded", timeout=60000)

    if bring_to_front:
        page.bring_to_front()
    return Session(pw=pw, browser=browser, page=page)


def open_deepseek(
    port: int = DEFAULT_PORT,
    profile: Path = DEFAULT_PROFILE,
    *,
    open_if_missing: bool = True,
    bring_to_front: bool = True,
) -> Session:
    """Return a session attached to a DeepSeek tab."""
    return open_chat_page(
        DEEPSEEK_URL,
        "chat.deepseek.com",
        port=port,
        profile=profile,
        open_if_missing=open_if_missing,
        bring_to_front=bring_to_front,
    )


def open_qwen(
    port: int = DEFAULT_PORT,
    profile: Path = DEFAULT_PROFILE,
    *,
    open_if_missing: bool = True,
    bring_to_front: bool = True,
) -> Session:
    """Return a session attached to a Qwen Studio tab."""
    return open_chat_page(
        QWEN_URL,
        "chat.qwen.ai",
        port=port,
        profile=profile,
        open_if_missing=open_if_missing,
        bring_to_front=bring_to_front,
    )


def open_mimo(
    port: int = DEFAULT_PORT,
    profile: Path = DEFAULT_PROFILE,
    *,
    open_if_missing: bool = True,
    bring_to_front: bool = True,
) -> Session:
    """Return a session attached to a Xiaomi MiMo Chat tab."""
    return open_chat_page(
        MIMO_URL,
        "aistudio.xiaomimimo.com",
        port=port,
        profile=profile,
        open_if_missing=open_if_missing,
        bring_to_front=bring_to_front,
    )
