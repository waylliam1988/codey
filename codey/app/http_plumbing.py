from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from codey import __version__
from codey.env_names import APP_VERSION_PLACEHOLDER, FRONTEND_ROOT_ENV

_DEFAULT_WEB_DIR = Path(__file__).resolve().parents[1] / "web"
_configured_web_dir = os.environ.get(FRONTEND_ROOT_ENV, "").strip()
WEB_DIR = Path(_configured_web_dir).expanduser().resolve() if _configured_web_dir else _DEFAULT_WEB_DIR
WEB_ASSET_TYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}
MAX_STATIC_CACHE_ENTRIES = 64
MAX_STATIC_CACHE_BYTES = 8 * 1024 * 1024
_STATIC_CACHE_LOCK = threading.Lock()
_STATIC_CACHE: OrderedDict[tuple[str, str], _StaticCacheEntry] = OrderedDict()
_STATIC_CACHE_BYTES = 0


@dataclass(frozen=True)
class _StaticCacheEntry:
    signature: tuple[int, int]
    body: bytes
    etag: str


def resolve_web_asset(url_path: str) -> tuple[Path, str] | None:
    """Resolve /assets/* to a real file inside codey/web/assets, or None."""
    prefix = "/assets/"
    if not url_path.startswith(prefix):
        return None
    name = url_path[len(prefix):]
    ctype = WEB_ASSET_TYPES.get(Path(name).suffix.lower())
    if not ctype:
        return None
    asset_dir = WEB_DIR / "assets"
    path = (asset_dir / name).resolve()
    try:
        path.relative_to(asset_dir.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path, ctype


def validate_web_root(root: Path | None = None) -> Path:
    """Return a complete static root or raise a clear configuration error."""
    candidate = (root or WEB_DIR).resolve()
    required = (candidate / "index.html", candidate / "assets")
    if not required[0].is_file() or not required[1].is_dir():
        raise RuntimeError(
            f"{FRONTEND_ROOT_ENV} must point to a built frontend root containing "
            f"index.html and assets/: {candidate}"
        )
    html = required[0].read_text(encoding="utf-8")
    if not re.search(r'<script[^>]+type=["\']module["\'][^>]+src=["\']/assets/[^"\']+\.js', html, re.I):
        raise RuntimeError(f"{FRONTEND_ROOT_ENV} index.html must contain a module script for a published JavaScript bundle: {candidate}")
    return candidate


def loopback_allowed_hosts(handler: BaseHTTPRequestHandler) -> set[str]:
    try:
        address = handler.server.server_address
        if not isinstance(address, tuple) or len(address) < 2:
            return set()
        port = address[1]
        bind_ip = str(address[0] or "")
    except Exception:
        return set()
    hosts = {
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"[::1]:{port}",
        "127.0.0.1",
        "localhost",
        "[::1]",
    }
    if bind_ip and bind_ip not in {"", "0.0.0.0", "::"}:
        hosts.add(f"{bind_ip}:{port}")
        hosts.add(bind_ip)
    return hosts


def request_allowed_origins(handler: BaseHTTPRequestHandler) -> set[str]:
    try:
        address = handler.server.server_address
        if not isinstance(address, tuple) or len(address) < 2:
            return set()
        port = address[1]
        bind_ip = str(address[0] or "")
    except Exception:
        return set()
    origins = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    }
    if bind_ip and bind_ip not in {"", "0.0.0.0", "::", "127.0.0.1", "::1"}:
        host = f"[{bind_ip}]" if ":" in bind_ip and not bind_ip.startswith("[") else bind_ip
        origins.add(f"http://{host}:{port}")
    return {item.lower() for item in origins}


def request_origin_allowed(handler: BaseHTTPRequestHandler) -> bool:
    host_header = (handler.headers.get("Host") or "").strip().lower()
    if host_header not in loopback_allowed_hosts(handler):
        return False
    origin = (handler.headers.get("Origin") or "").strip()
    if not origin:
        return True
    return origin.rstrip("/").lower() in request_allowed_origins(handler)


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def send_file(
    handler: BaseHTTPRequestHandler, path: Path, ctype: str, *, immutable: bool = False
) -> None:
    # Only version-pinned URLs (/assets/*?v=...) are immutable for a year:
    # the version query makes the URL content-addressed. Everything else
    # (icon, unpinned assets) stays revalidating so a changed file can never
    # stick behind a year-long max-age. index.html itself stays no-cache via
    # send_index().
    entry = _cached_file(path, transform_name="raw")
    if _request_etag_matches(handler, entry.etag):
        _send_not_modified(handler, entry.etag, immutable=immutable)
        return
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header(
        "Cache-Control",
        "public, max-age=31536000, immutable" if immutable else "no-cache",
    )
    handler.send_header("ETag", entry.etag)
    handler.send_header("Content-Length", str(len(entry.body)))
    handler.end_headers()
    handler.wfile.write(entry.body)


def send_index(handler: BaseHTTPRequestHandler) -> None:
    # Source checkouts can change without a package version bump. Include the
    # actual assets so existing desktop caches cannot mix old CSS/JS with new HTML.
    revision = hashlib.sha256()
    for asset in sorted((WEB_DIR / "assets").glob("*")):
        if asset.is_file() and asset.suffix in WEB_ASSET_TYPES:
            revision.update(asset.name.encode("utf-8"))
            revision.update(_cached_file(asset, transform_name="raw").body)
    asset_version = f"{__version__}-{revision.hexdigest()[:16]}"
    entry = _cached_file(WEB_DIR / "index.html", transform_name=f"index:{asset_version}")
    if _request_etag_matches(handler, entry.etag):
        _send_not_modified(handler, entry.etag)
        return
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("ETag", entry.etag)
    handler.send_header("Content-Length", str(len(entry.body)))
    handler.end_headers()
    handler.wfile.write(entry.body)


def _cached_file(path: Path, *, transform_name: str) -> _StaticCacheEntry:
    stat = path.stat()
    signature = (stat.st_mtime_ns, stat.st_size)
    key = (str(path), transform_name)
    with _STATIC_CACHE_LOCK:
        cached = _STATIC_CACHE.get(key)
        if cached is not None and cached.signature == signature:
            _STATIC_CACHE.move_to_end(key)
            return cached
    body = path.read_bytes()
    if transform_name.startswith("index:"):
        asset_version = transform_name.removeprefix("index:")
        body = body.decode("utf-8").replace(APP_VERSION_PLACEHOLDER, asset_version).encode("utf-8")
    entry = _StaticCacheEntry(
        signature=signature,
        body=body,
        etag=_static_etag(path, transform_name=transform_name, signature=signature),
    )
    with _STATIC_CACHE_LOCK:
        _put_static_cache_locked(key, entry)
    return entry


def _put_static_cache_locked(key: tuple[str, str], entry: _StaticCacheEntry) -> None:
    """Insert with entry+byte budgets; in-memory only, no disk fallback."""
    global _STATIC_CACHE_BYTES
    old = _STATIC_CACHE.get(key)
    if old is not None:
        _STATIC_CACHE_BYTES -= len(old.body)
        del _STATIC_CACHE[key]
    _STATIC_CACHE[key] = entry
    _STATIC_CACHE_BYTES += len(entry.body)
    while (
        len(_STATIC_CACHE) > MAX_STATIC_CACHE_ENTRIES
        or _STATIC_CACHE_BYTES > MAX_STATIC_CACHE_BYTES
    ) and _STATIC_CACHE:
        _old_key, evicted = _STATIC_CACHE.popitem(last=False)
        _STATIC_CACHE_BYTES -= len(evicted.body)
        if _old_key == key and not _STATIC_CACHE:
            break


def _static_etag(path: Path, *, transform_name: str, signature: tuple[int, int]) -> str:
    return f'W/"codey-{__version__}-{path.name}-{transform_name}-{signature[0]:x}-{signature[1]:x}"'


def _request_etag_matches(handler: BaseHTTPRequestHandler, etag: str) -> bool:
    header = (handler.headers.get("If-None-Match") or "")
    return any(item.strip() in {"*", etag} for item in header.split(","))


def _send_not_modified(handler: BaseHTTPRequestHandler, etag: str, *, immutable: bool = False) -> None:
    handler.send_response(304)
    handler.send_header(
        "Cache-Control",
        "public, max-age=31536000, immutable" if immutable else "no-cache",
    )
    handler.send_header("ETag", etag)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def parse_sse_event_id(value: object) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def sse_replay_cursor(value: object) -> int | None:
    parsed, _commit_seq = sse_cursor(value)
    return parsed if value is not None and parsed > 0 else None


def sse_cursor(value: object) -> tuple[int, int]:
    """Decode ``event-sequence:durable-commit-sequence`` SSE cursors.

    Numeric IDs remain readable as event-only cursors for clients from the
    previous release; they deliberately do not advance durable history.
    """
    text = str(value or "").strip()
    if ":" not in text:
        return parse_sse_event_id(text), 0
    event, commit = text.split(":", 1)
    return parse_sse_event_id(event), parse_sse_event_id(commit)


def write_sse_event(
    handler: BaseHTTPRequestHandler,
    event: dict[str, Any],
    *,
    event_id: int | str = 0,
) -> bool:
    try:
        data = json.dumps(dict(event), ensure_ascii=False)
        reset = event.get("type") == "resync_required" and event.get("cursor") == 0
        cursor = str(event_id)
        prefix = f"id: {cursor}\n" if cursor not in {"", "0"} or reset else ""
        handler.wfile.write(f"{prefix}data: {data}\n\n".encode())
        handler.wfile.flush()
        return True
    except Exception:
        return False


__all__ = [
    "WEB_DIR",
    "validate_web_root",
    "request_origin_allowed",
    "resolve_web_asset",
    "send_file",
    "send_index",
    "send_json",
    "sse_replay_cursor",
    "sse_cursor",
    "write_sse_event",
]
