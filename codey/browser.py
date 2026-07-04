"""Attach to long-lived Edge CDP tabs for supported web chat providers.

Codey treats the model browser as durable user state: closing or restarting the
local UI must not close Edge. Provider connections first reuse an existing CDP
browser and tab, then open a missing tab, and only launch Edge when no usable
CDP browser is available.
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

from codey import cancellation
from codey.local_store import DEFAULT_STATE_HOME

DEEPSEEK_URL = "https://chat.deepseek.com/"
QWEN_URL = "https://chat.qwen.ai/"
MIMO_URL = "https://aistudio.xiaomimimo.com/#/c"
GLM_URL = "https://chatglm.cn/main/alltoolsdetail?lang=zh"
DEFAULT_PORT = 9222
DEFAULT_PROFILE = DEFAULT_STATE_HOME / "edge-profile"
CDP_PORT_CANDIDATES = tuple(range(DEFAULT_PORT, DEFAULT_PORT + 17))
CDP_STATE_FILE = DEFAULT_STATE_HOME / "cdp-port.json"
PROVIDER_URL_CONTAINS = {
    "deepseek": "chat.deepseek.com",
    "qwen": "chat.qwen.ai",
    "mimo": "aistudio.xiaomimimo.com",
    "glm": "chatglm.cn",
}

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

_active_cdp_port: int | None = None


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
        cancellation.check()
        if _port_open(port):
            return
        cancellation.wait(0.3)
    raise TimeoutError(f"CDP port {port} did not open within {timeout:.0f}s")


def _load_saved_cdp_port() -> int | None:
    try:
        data = json.loads(CDP_STATE_FILE.read_text(encoding="utf-8"))
        port = int(data.get("port"))
    except Exception:
        return None
    if 1 <= port <= 65535:
        return port
    return None


def _save_cdp_port(port: int) -> None:
    try:
        CDP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CDP_STATE_FILE.write_text(json.dumps({"port": port}), encoding="utf-8")
    except OSError:
        pass


def _candidate_ports(preferred: int = DEFAULT_PORT) -> tuple[int, ...]:
    ports: list[int] = []
    near_preferred = range(preferred, min(preferred + 9, 65536))
    for item in (preferred, _active_cdp_port, _load_saved_cdp_port(), *near_preferred, *CDP_PORT_CANDIDATES):
        if item is None or item in ports:
            continue
        ports.append(int(item))
    return tuple(ports)


def _read_cdp_json(port: int, path: str, timeout: float = 1.0):
    if not _port_open(port):
        return None
    try:
        with urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _cdp_available(port: int = DEFAULT_PORT) -> bool:
    data = _read_cdp_json(port, "/json/version", timeout=0.8)
    return isinstance(data, dict) and bool(data.get("webSocketDebuggerUrl") or data.get("Browser"))


def list_cdp_targets(port: int = DEFAULT_PORT, timeout: float = 1.0) -> list[dict]:
    """Read Edge CDP targets without starting Playwright or opening pages."""
    data = _read_cdp_json(port, "/json/list", timeout=timeout)
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def detect_open_provider_tabs(port: int = DEFAULT_PORT) -> dict[str, bool]:
    statuses = {provider_id: False for provider_id in PROVIDER_URL_CONTAINS}
    for cdp_port in _candidate_ports(port):
        if not _cdp_available(cdp_port):
            continue
        for target in list_cdp_targets(cdp_port):
            if str(target.get("type") or "") != "page":
                continue
            url = str(target.get("url") or "")
            for provider_id, marker in PROVIDER_URL_CONTAINS.items():
                if marker in url:
                    statuses[provider_id] = True
    return statuses


def _remember_cdp_port(port: int) -> int:
    global _active_cdp_port
    _active_cdp_port = port
    _save_cdp_port(port)
    return port


def _find_cdp_port_with_target(url_contains: str, preferred: int = DEFAULT_PORT) -> int | None:
    for cdp_port in _candidate_ports(preferred):
        if not _cdp_available(cdp_port):
            continue
        for target in list_cdp_targets(cdp_port):
            if str(target.get("type") or "") == "page" and url_contains in str(target.get("url") or ""):
                return _remember_cdp_port(cdp_port)
    return None


def _find_existing_cdp_port(preferred: int = DEFAULT_PORT) -> int | None:
    for cdp_port in _candidate_ports(preferred):
        if _cdp_available(cdp_port):
            return _remember_cdp_port(cdp_port)
    return None


def _find_free_cdp_port(preferred: int = DEFAULT_PORT) -> int:
    for cdp_port in _candidate_ports(preferred):
        if not _port_open(cdp_port):
            return cdp_port
    raise RuntimeError("no free CDP port available")


def _ensure_cdp_port(
    *,
    preferred: int,
    profile: Path,
    start_url: str,
    url_contains: str,
    open_if_missing: bool,
) -> int:
    existing = _find_cdp_port_with_target(url_contains, preferred)
    if existing is not None:
        return existing

    existing = _find_existing_cdp_port(preferred)
    if existing is not None:
        return existing

    if not open_if_missing:
        raise RuntimeError(f"CDP port {preferred} is not open")

    cdp_port = _find_free_cdp_port(preferred)
    _launch_edge(cdp_port, profile, start_url)
    _wait_port(cdp_port)
    return _remember_cdp_port(cdp_port)


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
        cancellation.check()
        try:
            return sync_playwright().start()
        except AttributeError as exc:
            if not _is_playwright_startup_race(exc):
                raise
            last_error = exc
            cancellation.wait(0.25 * (attempt + 1))
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
    cancellation.check()
    cdp_port = _ensure_cdp_port(
        preferred=port,
        profile=profile,
        start_url=start_url,
        url_contains=url_contains,
        open_if_missing=open_if_missing,
    )

    pw = _start_playwright_with_retry()
    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
    _check_cancelled_connection(pw)

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
        _check_cancelled_connection(pw)
        page.goto(start_url, wait_until="domcontentloaded", timeout=60000)

    if bring_to_front:
        _check_cancelled_connection(pw)
        page.bring_to_front()
    _check_cancelled_connection(pw)
    return Session(pw=pw, browser=browser, page=page)


def _check_cancelled_connection(pw: Playwright) -> None:
    try:
        cancellation.check()
    except cancellation.TaskCancelled:
        pw.stop()
        raise


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


def open_glm(
    port: int = DEFAULT_PORT,
    profile: Path = DEFAULT_PROFILE,
    *,
    open_if_missing: bool = True,
    bring_to_front: bool = True,
) -> Session:
    """Return a session attached to a GLM tab."""
    return open_chat_page(
        GLM_URL,
        "chatglm.cn",
        port=port,
        profile=profile,
        open_if_missing=open_if_missing,
        bring_to_front=bring_to_front,
    )
