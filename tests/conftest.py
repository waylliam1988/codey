from __future__ import annotations

import atexit
import os
import sys
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def public_source_dns(monkeypatch):
    """Resolve scripted sources locally while retaining the real URL policy."""
    import socket
    from types import SimpleNamespace

    from codey.policies import network

    def resolve(host, port, **_kwargs):
        assert host == "example.com", f"No scripted DNS answer for {host}"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

    monkeypatch.setattr(network, "socket", SimpleNamespace(getaddrinfo=resolve, IPPROTO_TCP=socket.IPPROTO_TCP))
    monkeypatch.setattr(network, "DEFAULT_NETWORK_POLICY", network.NetworkPolicy())


@pytest.fixture
def scripted_local_api_connection(monkeypatch):
    """A non-network admission scope for tests that inject a scripted provider."""
    from codey.providers import local_selection
    from codey.providers.local_config import LocalProviderConfig

    config = LocalProviderConfig(base_url="http://scripted.test/v1", model="scripted-fixture", connection_revision="scripted-fixture")
    monkeypatch.setattr("codey.providers.local_config.load_local_config", lambda: config)
    monkeypatch.setattr(local_selection, "load_local_config", lambda: config)
    monkeypatch.setattr("codey.providers.local_tokens._metadata_json", lambda *_: {})


@pytest.fixture
def no_external_advisor_models(monkeypatch):
    """Scripted entry tests select real services without probing user models."""
    from codey.app.provider_services import reset_provider_availability_cache

    reset_provider_availability_cache()
    monkeypatch.setattr("codey.app.provider_services.provider_tab_availability", lambda **_: {})
    try:
        yield
    finally:
        reset_provider_availability_cache()


def _preserve_playwright_browser_cache() -> None:
    """Keep installed browsers outside the isolated application-state home."""
    # Match Playwright's environment precedence, including package-local "0".
    setting = next((os.environ[name] for name in (
        "PLAYWRIGHT_BROWSERS_PATH", "npm_config_playwright_browsers_path",
        "npm_package_config_playwright_browsers_path",
    ) if name in os.environ), None)
    if setting:
        return
    home = Path.home()
    if sys.platform == "linux":
        cache = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    elif sys.platform == "darwin":
        cache = home / "Library" / "Caches"
    elif sys.platform == "win32":
        cache = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    else:
        return
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(cache / "ms-playwright")


def _install_isolated_state_home() -> Path:
    """Give every pytest process a writable home before application imports.

    Codey's default stores are module constants derived from ``Path.home()``.
    This must run while conftest is imported, before test collection imports
    application modules; a later fixture would leave cached defaults pointing
    at the real user profile.
    """
    _preserve_playwright_browser_cache()
    existing = os.environ.get("PYTEST_STATE_HOME")
    if existing:
        root = Path(existing).resolve()
    else:
        root = Path(tempfile.mkdtemp(prefix="codey-pytest-home-")).resolve()
        os.environ["PYTEST_STATE_HOME"] = str(root)
        atexit.register(lambda: _remove_test_home(root))
    root.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(root)
    if os.name == "nt":
        os.environ["USERPROFILE"] = str(root)
        os.environ["HOMEDRIVE"] = root.drive or Path(str(root)).drive
        # Full temp-home remainder after the drive (e.g. \Users\...\Temp\...),
        # never the bare anchor "\", so child processes resolve the same home.
        try:
            remainder = str(root)[len(os.environ["HOMEDRIVE"]):] or "\\"
        except Exception:
            remainder = "\\"
        if not remainder.startswith("\\"):
            remainder = "\\" + remainder
        os.environ["HOMEPATH"] = remainder
    return root


def _remove_test_home(root: Path) -> None:
    import shutil

    shutil.rmtree(root, ignore_errors=True)


_PYTEST_STATE_HOME = _install_isolated_state_home()


def _is_pytest_current_cleanup_permission_error(root: Path, exc: PermissionError) -> bool:
    try:
        root_parts = Path(root).parts
    except (OSError, TypeError, ValueError):
        root_parts = ()
    return "pytest-current" in root_parts or "pytest-current" in str(exc)


if os.name == "nt":
    import _pytest.pathlib as _pytest_pathlib

    _cleanup_dead_symlinks = _pytest_pathlib.cleanup_dead_symlinks

    def _windows_cleanup_dead_symlinks(root: Path) -> None:
        # Pytest may hit PermissionError resolving pytest-current symlinks on Windows.
        try:
            _cleanup_dead_symlinks(root)
        except PermissionError as exc:
            if _is_pytest_current_cleanup_permission_error(root, exc):
                return
            raise

    _pytest_pathlib.cleanup_dead_symlinks = _windows_cleanup_dead_symlinks
