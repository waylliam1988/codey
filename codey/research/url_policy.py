"""URL policy for Research web reads."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
}
_DNS_FAKE_IP_NETS = (
    ipaddress.ip_network("198.18.0.0/15"),
)


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    return not ip.is_global or ip.is_multicast


def _ip_is_dns_fake_ip(ip: ipaddress._BaseAddress) -> bool:
    return any(ip in net for net in _DNS_FAKE_IP_NETS)


def check_fetch_url(url: str, *, resolve: bool = True) -> str | None:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        return "only http(s) URLs are allowed"
    host = parsed.hostname
    if not host:
        return "URL has no host"
    if host.lower() in _BLOCKED_HOSTS:
        return "refusing to open a local/loopback address"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return "refusing to open a non-public address" if _ip_is_blocked(ip) else None
    if not resolve:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return "could not resolve host"
    for info in infos:
        address = info[4][0].split("%")[0]
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError:
            continue
        if _ip_is_blocked(resolved) and not _ip_is_dns_fake_ip(resolved):
            return "refusing to open a non-public address"
    return None
