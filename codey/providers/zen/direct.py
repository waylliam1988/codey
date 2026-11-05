"""A loopback CONNECT tunnel bound to a physical Windows interface.

Only this process's sockets and DNS queries are bound. No VPN controller,
configuration, system proxy, DNS setting or route table is modified.
"""
from __future__ import annotations

import base64
import contextlib
import hmac
import ipaddress
import json
import os
import secrets
import select
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from codey.providers.api_transport import GenerationNotSentError

_INTERFACE_SCRIPT = r"""
$ErrorActionPreference='Stop'
$items = @(Get-NetIPConfiguration | Where-Object {
  $_.NetAdapter.HardwareInterface -and $_.NetAdapter.Status -eq 'Up' -and $_.IPv4DefaultGateway
} | Sort-Object { $_.NetIPv4Interface.InterfaceMetric } | ForEach-Object {
  $dns=Get-DnsClientServerAddress -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4
  [pscustomobject]@{index=$_.InterfaceIndex; address=$_.IPv4Address[0].IPAddress; dns=@($dns.ServerAddresses)}
})
ConvertTo-Json -InputObject $items -Compress
"""


def _check(deadline: float, cancelled: Callable[[], bool]) -> None:
    if cancelled() or time.monotonic() >= deadline:
        raise GenerationNotSentError("Zen direct connection cancelled or timed out")


@dataclass(frozen=True)
class PhysicalInterface:
    index: int
    address: str
    dns: tuple[str, ...]

    def socket(self, kind: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, kind)
        try:
            # Windows IP_UNICAST_IF takes the interface index in network order.
            sock.setsockopt(socket.IPPROTO_IP, 31, struct.pack("!I", self.index))
            sock.bind((self.address, 0))
            return sock
        except BaseException:
            sock.close()
            raise


def discover_interface(deadline: float, cancelled: Callable[[], bool]) -> PhysicalInterface:
    if os.name != "nt":
        raise GenerationNotSentError("Zen physical-interface fallback is unavailable on this platform")
    _check(deadline, cancelled)
    proc = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _INTERFACE_SCRIPT],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        until = min(deadline, time.monotonic() + 12)
        while True:
            _check(until, cancelled)
            try:
                stdout, _ = proc.communicate(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                continue
        try:
            rows = json.loads(stdout) if proc.returncode == 0 and len(stdout) < 32768 else []
        except ValueError as exc:
            raise GenerationNotSentError("Zen physical-interface query returned invalid data") from exc
        for row in rows:
            address = str(ipaddress.IPv4Address(row["address"]))
            dns = tuple(str(ipaddress.IPv4Address(value)) for value in row["dns"])
            if type(row["index"]) is int and row["index"] > 0 and dns:
                return PhysicalInterface(row["index"], address, dns)
        raise GenerationNotSentError("Zen could not find an active physical interface with IPv4 DNS")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=2)


def _skip_name(data: bytes, offset: int) -> int:
    for _ in range(128):
        size = data[offset]
        if size & 0xC0 == 0xC0:
            if offset + 2 > len(data):
                raise ValueError("truncated DNS pointer")
            return offset + 2
        offset += 1
        if not size:
            return offset
        if size > 63 or offset + size > len(data):
            raise ValueError("invalid DNS name")
        offset += size
    raise ValueError("oversized DNS name")


def dns_answers(data: bytes, query_id: int) -> tuple[str, ...]:
    identity, flags, questions, answers, _, _ = struct.unpack_from("!6H", data)
    if identity != query_id or not flags & 0x8000 or flags & 0x020F or questions != 1 or answers > 64:
        raise ValueError("invalid DNS response")
    offset = _skip_name(data, 12) + 4
    result = []
    for _ in range(answers):
        offset = _skip_name(data, offset)
        kind, domain, _, size = struct.unpack_from("!HHIH", data, offset)
        offset += 10
        if offset + size > len(data):
            raise ValueError("truncated DNS answer")
        if kind == 1 and domain == 1 and size == 4:
            address = ipaddress.IPv4Address(data[offset:offset + size])
            if address.is_global:
                result.append(str(address))
        offset += size
    if not result:
        raise ValueError("DNS supplied no public IPv4 address")
    return tuple(result)


def resolve(interface: PhysicalInterface, host: str, deadline: float, cancelled: Callable[[], bool]) -> tuple[str, ...]:
    identity = secrets.randbits(16)
    packet = struct.pack("!6H", identity, 0x100, 1, 0, 0, 0)
    packet += b"".join(bytes([len(label)]) + label.encode("ascii") for label in host.split(".")) + b"\0\0\1\0\1"
    for server in interface.dns:
        _check(deadline, cancelled)
        try:
            with interface.socket(socket.SOCK_DGRAM) as sock:
                sock.settimeout(min(1.5, max(0.01, deadline - time.monotonic())))
                sock.connect((server, 53))
                sock.send(packet)
                return dns_answers(sock.recv(4096), identity)
        except (OSError, ValueError, IndexError, struct.error):
            continue
    raise GenerationNotSentError("Zen physical-interface DNS lookup failed")


class DirectTunnel:
    """One authenticated, host-scoped TLS byte tunnel, owned by one logical request."""

    def __init__(self, host: str, deadline: float, cancelled: Callable[[], bool], *,
                 interface: PhysicalInterface | None = None) -> None:
        if host not in {"opencode.ai", "api.opencode.ai"}:
            raise GenerationNotSentError("Zen direct destination is not approved")
        self.interface = interface or discover_interface(deadline, cancelled)
        self.addresses = resolve(self.interface, host, deadline, cancelled)
        self.host, self.deadline, self.cancelled = host, deadline, cancelled
        self._sockets: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        token = secrets.token_urlsafe(24)
        self._authorization = "Basic " + base64.b64encode(("codey:" + token).encode()).decode()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                super().setup()
                owner._track(self.connection)
                self.connection.settimeout(5)

            def finish(self) -> None:
                try:
                    super().finish()
                finally:
                    with owner._lock:
                        owner._sockets.discard(self.connection)

            def log_message(self, *_: object) -> None:
                pass

            def do_CONNECT(self) -> None:
                try:
                    target = urlsplit("//" + self.path)
                    port = target.port
                except ValueError:
                    self.send_error(403)
                    return
                if (not hmac.compare_digest(self.headers.get("Proxy-Authorization", ""), owner._authorization)
                        or target.hostname != owner.host or port != 443 or target.username or target.password
                        or target.path or target.query or target.fragment):
                    self.send_error(403)
                    return
                owner._forward(self)

        class Server(ThreadingHTTPServer):
            def handle_error(self, request: socket.socket | tuple[bytes, socket.socket], client_address: object) -> None:
                if not owner._closed.is_set():
                    super().handle_error(request, client_address)

        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.proxy_url = f"http://codey:{token}@127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True)
        self.thread.start()

    def _track(self, sock: socket.socket) -> None:
        with self._lock:
            if self._closed.is_set():
                sock.close()
                raise GenerationNotSentError("Zen direct tunnel is closed")
            self._sockets.add(sock)

    def _connect(self) -> socket.socket:
        for address in self.addresses:
            _check(self.deadline, lambda: self._closed.is_set() or self.cancelled())
            sock = self.interface.socket(socket.SOCK_STREAM)
            try:
                self._track(sock)
                sock.settimeout(min(2.0, max(0.01, self.deadline - time.monotonic())))
                sock.connect((address, 443))
                return sock
            except OSError:
                with self._lock:
                    self._sockets.discard(sock)
                sock.close()
        raise OSError("Zen physical interface could not reach the service")

    def _forward(self, handler: BaseHTTPRequestHandler) -> None:
        remote = None
        try:
            remote = self._connect()
            self._track(remote)
            handler.send_response(200, "Connection established")
            handler.end_headers()
            handler.wfile.flush()
            while not self._closed.is_set() and not self.cancelled() and time.monotonic() < self.deadline:
                readable, _, _ = select.select([handler.connection, remote], [], [], 0.05)
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    (remote if source is handler.connection else handler.connection).sendall(data)
        except (OSError, GenerationNotSentError):
            return
        finally:
            with self._lock:
                self._sockets.discard(handler.connection)
                if remote is not None:
                    self._sockets.discard(remote)
                    remote.close()

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            for sock in self._sockets:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                sock.close()
            self._sockets.clear()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)
