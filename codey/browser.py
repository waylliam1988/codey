"""Attach to long-lived Chromium CDP tabs for supported web chat providers.

Codey treats the model browser as durable user state: closing or restarting the
local UI must not close the model browser. Provider connections first reuse an
existing CDP browser and tab, then open a missing tab, and only launch Edge or
Chrome when no usable CDP browser is available.
"""

from __future__ import annotations

import json
import os
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
STEPFUN_URL = "https://chat.stepfun.com/chats/"
GLM_URL = "https://chatglm.cn/main/alltoolsdetail?lang=zh"
DEFAULT_PORT = 9222
CDP_CONNECT_TIMEOUT_MS = 60_000
WARMUP_CDP_CONNECT_TIMEOUT_MS = 8_000
WARMUP_NAVIGATION_TIMEOUT_MS = 10_000
WARMUP_PORT_TIMEOUT = 10.0
EDGE_PROFILE = DEFAULT_STATE_HOME / "edge-profile"
CHROME_PROFILE = DEFAULT_STATE_HOME / "chrome-profile"
DEFAULT_PROFILE = EDGE_PROFILE
CDP_PORT_CANDIDATES = tuple(range(DEFAULT_PORT, DEFAULT_PORT + 17))
CDP_STATE_FILE = DEFAULT_STATE_HOME / "cdp-port.json"
PROVIDER_URL_CONTAINS = {
    "deepseek": "chat.deepseek.com",
    "qwen": "chat.qwen.ai",
    "mimo": "aistudio.xiaomimimo.com",
    "stepfun": "chat.stepfun.com",
    "glm": "chatglm.cn",
}
PROVIDER_START_URLS = {
    "deepseek": DEEPSEEK_URL,
    "qwen": QWEN_URL,
    "mimo": MIMO_URL,
    "stepfun": STEPFUN_URL,
    "glm": GLM_URL,
}

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
CODEY_BROWSER_PATH_ENV = "CODEY_BROWSER_PATH"

_active_cdp_port: int | None = None


@dataclass(frozen=True)
class BrowserExecutable:
    path: Path
    kind: str
    profile: Path


@dataclass(frozen=True)
class CdpEndpoint:
    port: int
    process: subprocess.Popen | None = None


def _browser_profile(kind: str) -> Path:
    return CHROME_PROFILE if kind == "chrome" else EDGE_PROFILE


def _find_browser() -> BrowserExecutable:
    configured = os.environ.get(CODEY_BROWSER_PATH_ENV, "").strip().strip('"')
    if configured:
        path = Path(configured)
        if path.is_file():
            name = path.name.lower()
            kind = "chrome" if "chrome" in name and "edge" not in name else "edge"
            return BrowserExecutable(path, kind, _browser_profile(kind))
        raise FileNotFoundError(
            f"{CODEY_BROWSER_PATH_ENV} points to a missing browser executable: {configured}"
        )
    for p in EDGE_PATHS:
        path = Path(p)
        if path.is_file():
            return BrowserExecutable(path, "edge", EDGE_PROFILE)
    for p in CHROME_PATHS:
        path = Path(p)
        if path.is_file():
            return BrowserExecutable(path, "chrome", CHROME_PROFILE)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        path = Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe"
        if path.is_file():
            return BrowserExecutable(path, "chrome", CHROME_PROFILE)
    raise FileNotFoundError(
        "Could not find Microsoft Edge or Google Chrome. Install one of them, "
        f"or set {CODEY_BROWSER_PATH_ENV} to the browser executable."
    )


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _launch_browser(
    port: int,
    profile: Path | None,
    start_url: str,
    *,
    browser_path: str | Path | None = None,
) -> subprocess.Popen:
    if browser_path:
        executable = Path(browser_path).expanduser()
        if not executable.is_file():
            raise FileNotFoundError(f"browser executable not found: {executable}")
        default_profile = DEFAULT_PROFILE
    else:
        browser = _find_browser()
        executable = browser.path
        default_profile = browser.profile
    profile_path = (
        default_profile
        if profile is None or Path(profile) == DEFAULT_PROFILE
        else Path(profile)
    )
    profile_path.mkdir(parents=True, exist_ok=True)
    args = [
        str(executable),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_path}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    args.append(start_url)
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
    """Read Chromium CDP targets without starting Playwright or opening pages."""
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


def _remembered_cdp_ports(preferred: int = DEFAULT_PORT) -> tuple[int, ...]:
    ports: list[int] = []
    if preferred == DEFAULT_PORT:
        allowed_ports = set(CDP_PORT_CANDIDATES)
    else:
        allowed_ports = set(range(preferred, min(preferred + 9, 65536)))
    for item in (_active_cdp_port, _load_saved_cdp_port()):
        if item is None or item in ports:
            continue
        port = int(item)
        if port in allowed_ports:
            ports.append(port)
    return tuple(ports)


def _find_remembered_cdp_port(preferred: int = DEFAULT_PORT) -> int | None:
    for cdp_port in _remembered_cdp_ports(preferred):
        if _cdp_available(cdp_port):
            return _remember_cdp_port(cdp_port)
    return None


def _find_free_cdp_port(preferred: int = DEFAULT_PORT) -> int:
    for cdp_port in _candidate_ports(preferred):
        if not _port_open(cdp_port):
            return cdp_port
    raise RuntimeError("no free CDP port available")


def _find_free_isolated_cdp_port(preferred: int = DEFAULT_PORT) -> int:
    near_preferred = range(preferred, min(preferred + 9, 65536))
    ports = tuple(dict.fromkeys((preferred, *near_preferred, *CDP_PORT_CANDIDATES)))
    for cdp_port in ports:
        if not _port_open(cdp_port):
            return cdp_port
    raise RuntimeError("no free isolated CDP port available")


def _ensure_cdp_port(
    **kwargs,
) -> int:
    return _ensure_cdp_endpoint(**kwargs).port


def _ensure_cdp_endpoint(
    *,
    preferred: int,
    profile: Path,
    start_url: str,
    url_contains: str,
    open_if_missing: bool,
    isolated: bool = False,
    browser_path: str | Path | None = None,
) -> CdpEndpoint:
    launch_kwargs = {}
    if browser_path is not None:
        launch_kwargs["browser_path"] = browser_path
    if isolated:
        if not open_if_missing:
            raise RuntimeError("isolated provider sessions require open_if_missing=True")
        cdp_port = _find_free_isolated_cdp_port(preferred)
        process = _launch_browser(cdp_port, profile, start_url, **launch_kwargs)
        try:
            _wait_port(cdp_port)
        except Exception:
            _terminate_browser_process(process)
            raise
        return CdpEndpoint(cdp_port, process)

    existing = _find_cdp_port_with_target(url_contains, preferred)
    if existing is not None:
        return CdpEndpoint(existing)

    existing = _find_existing_cdp_port(preferred)
    if existing is not None:
        return CdpEndpoint(existing)

    if not open_if_missing:
        raise RuntimeError(f"CDP port {preferred} is not open")

    cdp_port = _find_free_cdp_port(preferred)
    process = _launch_browser(cdp_port, profile, start_url, **launch_kwargs)
    try:
        _wait_port(cdp_port)
    except Exception:
        _terminate_browser_process(process)
        raise
    return CdpEndpoint(_remember_cdp_port(cdp_port))


def _ensure_cdp_browser_endpoint(
    *,
    preferred: int,
    profile: Path,
    start_url: str,
    open_if_missing: bool = True,
    wait_timeout: float = 20.0,
    browser_path: str | Path | None = None,
) -> CdpEndpoint:
    existing = _find_remembered_cdp_port(preferred)
    if existing is not None:
        return CdpEndpoint(existing)

    if not open_if_missing:
        raise RuntimeError(f"CDP port {preferred} is not open")

    cdp_port = _find_free_cdp_port(preferred)
    launch_kwargs = {}
    if browser_path is not None:
        launch_kwargs["browser_path"] = browser_path
    process = _launch_browser(cdp_port, profile, start_url, **launch_kwargs)
    try:
        _wait_port(cdp_port, timeout=wait_timeout)
    except Exception:
        _terminate_browser_process(process)
        raise
    return CdpEndpoint(_remember_cdp_port(cdp_port), process)


def _page_urls(browser: Browser) -> tuple[str, ...]:
    urls: list[str] = []
    for ctx in browser.contexts:
        for page in ctx.pages:
            try:
                urls.append(str(page.url or ""))
            except Exception:
                pass
    return tuple(urls)


def _has_provider_url(urls: tuple[str, ...], provider_id: str) -> bool:
    marker = PROVIDER_URL_CONTAINS.get(provider_id)
    return bool(marker) and any(marker in url for url in urls)


def _close_failed_warmup_page(page: Page | None, provider_id: str) -> None:
    if page is None:
        return
    marker = PROVIDER_URL_CONTAINS.get(provider_id)
    try:
        url = str(page.url or "")
    except Exception:
        url = ""
    if marker and marker in url:
        return
    try:
        page.close()
    except Exception:
        pass


def warm_provider_tabs(
    *,
    port: int = DEFAULT_PORT,
    profile: Path = DEFAULT_PROFILE,
) -> dict[str, bool]:
    """Best-effort provider tab warmup without login or readiness checks."""
    statuses = detect_open_provider_tabs(port)
    if any(statuses.values()):
        return statuses

    first_url = next(iter(PROVIDER_START_URLS.values()))
    endpoint = _ensure_cdp_browser_endpoint(
        preferred=port,
        profile=profile,
        start_url=first_url,
        wait_timeout=WARMUP_PORT_TIMEOUT,
    )

    pw: Playwright | None = None
    try:
        pw = _start_playwright_with_retry()
        browser = pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{endpoint.port}",
            timeout=WARMUP_CDP_CONNECT_TIMEOUT_MS,
        )
        _check_cancelled_connection(pw)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        urls = _page_urls(browser)

        for provider_id, url in PROVIDER_START_URLS.items():
            if _has_provider_url(urls, provider_id):
                continue
            page: Page | None = None
            try:
                page = ctx.new_page()
                _check_cancelled_connection(pw)
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=WARMUP_NAVIGATION_TIMEOUT_MS,
                )
                urls = (*urls, str(page.url or url))
            except cancellation.TaskCancelled:
                _close_failed_warmup_page(page, provider_id)
                raise
            except Exception:
                _close_failed_warmup_page(page, provider_id)
                continue
        _check_cancelled_connection(pw)
    finally:
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass

    return detect_open_provider_tabs(endpoint.port)


@dataclass
class Session:
    pw: Playwright
    browser: Browser
    page: Page
    process: subprocess.Popen | None = None
    close_page_on_close: bool = False
    cdp_port: int = 0

    def close(self) -> None:
        try:
            if self.close_page_on_close:
                try:
                    self.page.close()
                except Exception:
                    pass
            self.pw.stop()
        finally:
            if self.process is not None:
                _terminate_browser_process(self.process)


def _terminate_browser_process(process: subprocess.Popen) -> None:
    try:
        if process.poll() is not None:
            return
        process.terminate()
        process.wait(timeout=2)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=2)
        except Exception:
            pass


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
        "Playwright failed to initialize. Close stale Codey browser automation "
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
    isolated: bool = False,
    fresh_tab: bool = False,
    browser_path: str | Path | None = None,
) -> Session:
    """Return a Playwright session attached to a matching provider tab."""
    cancellation.check()
    if fresh_tab and not open_if_missing:
        raise RuntimeError("fresh provider tabs require open_if_missing=True")
    endpoint = _ensure_cdp_endpoint(
        preferred=port,
        profile=profile,
        start_url="about:blank" if fresh_tab else start_url,
        url_contains=url_contains,
        open_if_missing=open_if_missing,
        isolated=isolated,
        browser_path=browser_path,
    )

    pw: Playwright | None = None
    try:
        pw = _start_playwright_with_retry()
        browser = pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{endpoint.port}",
            timeout=CDP_CONNECT_TIMEOUT_MS,
        )
        _check_cancelled_connection(pw)

        page: Page | None = None
        if not fresh_tab:
            for ctx in browser.contexts:
                for p in ctx.pages:
                    if url_contains in (p.url or ""):
                        page = p
                        break
                if page:
                    break

        if page is None:
            if not open_if_missing:
                raise RuntimeError(f"no existing provider tab matched {url_contains}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            _check_cancelled_connection(pw)
            page.goto(start_url, wait_until="domcontentloaded", timeout=60000)

        if bring_to_front:
            _check_cancelled_connection(pw)
            page.bring_to_front()
        _check_cancelled_connection(pw)
        return Session(
            pw=pw,
            browser=browser,
            page=page,
            process=endpoint.process,
            close_page_on_close=fresh_tab,
            cdp_port=endpoint.port,
        )
    except Exception:
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        if endpoint.process is not None:
            _terminate_browser_process(endpoint.process)
        raise


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
    isolated: bool = False,
    fresh_tab: bool = False,
) -> Session:
    """Return a session attached to a DeepSeek tab."""
    return open_chat_page(
        DEEPSEEK_URL,
        "chat.deepseek.com",
        port=port,
        profile=profile,
        open_if_missing=open_if_missing,
        bring_to_front=bring_to_front,
        isolated=isolated,
        fresh_tab=fresh_tab,
    )


def open_qwen(
    port: int = DEFAULT_PORT,
    profile: Path = DEFAULT_PROFILE,
    *,
    open_if_missing: bool = True,
    bring_to_front: bool = True,
    isolated: bool = False,
    fresh_tab: bool = False,
) -> Session:
    """Return a session attached to a Qwen Studio tab."""
    return open_chat_page(
        QWEN_URL,
        "chat.qwen.ai",
        port=port,
        profile=profile,
        open_if_missing=open_if_missing,
        bring_to_front=bring_to_front,
        isolated=isolated,
        fresh_tab=fresh_tab,
    )


def open_mimo(
    port: int = DEFAULT_PORT,
    profile: Path = DEFAULT_PROFILE,
    *,
    open_if_missing: bool = True,
    bring_to_front: bool = True,
    isolated: bool = False,
    fresh_tab: bool = False,
) -> Session:
    """Return a session attached to a Xiaomi MiMo Chat tab."""
    return open_chat_page(
        MIMO_URL,
        "aistudio.xiaomimimo.com",
        port=port,
        profile=profile,
        open_if_missing=open_if_missing,
        bring_to_front=bring_to_front,
        isolated=isolated,
        fresh_tab=fresh_tab,
    )


def open_stepfun(
    port: int = DEFAULT_PORT,
    profile: Path = DEFAULT_PROFILE,
    *,
    open_if_missing: bool = True,
    bring_to_front: bool = True,
    isolated: bool = False,
    fresh_tab: bool = False,
) -> Session:
    """Return a session attached to a StepFun Chat tab."""
    return open_chat_page(
        STEPFUN_URL,
        "chat.stepfun.com",
        port=port,
        profile=profile,
        open_if_missing=open_if_missing,
        bring_to_front=bring_to_front,
        isolated=isolated,
        fresh_tab=fresh_tab,
    )


def open_glm(
    port: int = DEFAULT_PORT,
    profile: Path = DEFAULT_PROFILE,
    *,
    open_if_missing: bool = True,
    bring_to_front: bool = True,
    isolated: bool = False,
    fresh_tab: bool = False,
) -> Session:
    """Return a session attached to a GLM tab."""
    return open_chat_page(
        GLM_URL,
        "chatglm.cn",
        port=port,
        profile=profile,
        open_if_missing=open_if_missing,
        bring_to_front=bring_to_front,
        isolated=isolated,
        fresh_tab=fresh_tab,
    )
