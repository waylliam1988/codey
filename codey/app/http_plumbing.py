from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from codey import __version__


WEB_DIR = Path(__file__).resolve().parents[1] / "web"
WEB_ASSET_DIR = WEB_DIR / "assets"
WEB_ASSET_TYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def resolve_web_asset(url_path: str) -> tuple[Path, str] | None:
    """Resolve /assets/* to a real file inside codey/web/assets, or None."""
    prefix = "/assets/"
    if not url_path.startswith(prefix):
        return None
    name = url_path[len(prefix):]
    ctype = WEB_ASSET_TYPES.get(Path(name).suffix.lower())
    if not ctype:
        return None
    path = (WEB_ASSET_DIR / name).resolve()
    try:
        path.relative_to(WEB_ASSET_DIR.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path, ctype


def loopback_allowed_hosts(handler: BaseHTTPRequestHandler) -> set[str]:
    try:
        port = handler.server.server_address[1]
        bind_ip = str(handler.server.server_address[0] or "")
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
        port = handler.server.server_address[1]
        bind_ip = str(handler.server.server_address[0] or "")
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
    host_header = str(handler.headers.get("Host") or "").strip().lower()
    if host_header not in loopback_allowed_hosts(handler):
        return False
    origin = str(handler.headers.get("Origin") or "").strip()
    if not origin:
        return True
    return origin.rstrip("/").lower() in request_allowed_origins(handler)


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def send_file(handler: BaseHTTPRequestHandler, path: Path, ctype: str) -> None:
    data = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def send_index(handler: BaseHTTPRequestHandler) -> None:
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("__CODEY_VERSION__", __version__)
    body = html.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def parse_sse_event_id(value: object) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def sse_replay_cursor(value: object) -> int | None:
    parsed = parse_sse_event_id(value)
    return parsed if value is not None and parsed > 0 else None


def write_sse_event(
    handler: BaseHTTPRequestHandler,
    event: dict,
    *,
    event_id: int = 0,
) -> bool:
    data = json.dumps(dict(event), ensure_ascii=False)
    prefix = f"id: {event_id}\n" if event_id > 0 else ""
    try:
        handler.wfile.write(f"{prefix}data: {data}\n\n".encode("utf-8"))
        handler.wfile.flush()
        return True
    except Exception:
        return False


__all__ = [
    "WEB_DIR",
    "request_origin_allowed",
    "resolve_web_asset",
    "send_file",
    "send_index",
    "send_json",
    "sse_replay_cursor",
    "write_sse_event",
]
