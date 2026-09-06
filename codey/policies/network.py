"""Unified network policy and address verification for research and web reads.

This policy lowers SSRF risk by preventing requests to local, private, and
unresolved network endpoints. Note that this is an application-level SSRF guard
and not a hard DNS-rebinding sandbox, as browser navigation engines resolve
connections independently.

DNS-resolved 198.18.0.0/15 addresses are treated as policy-allowed by default
for TUN/transparent-proxy setups. That compatibility path is not proof that the
destination is a globally routed public host; it only means the URL passed this
application's configured fetch policy. Literal 198.18.0.0/15 IP URLs remain
blocked.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import ipaddress
import socket
import threading
import time
from urllib.parse import urlparse

from codey.utils.refs import is_valid_hostname

_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
    }
)

_DNS_FAKE_IP_NETS = (ipaddress.ip_network("198.18.0.0/15"),)


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not ip.is_global or ip.is_multicast


def _ip_is_dns_fake_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(ip in net for net in _DNS_FAKE_IP_NETS)


class NetworkStatus(Enum):
    # POLICY_ALLOWED means "allowed by this policy", not a hard proof that DNS
    # resolved to a globally routed address in all local proxy configurations.
    POLICY_ALLOWED = "policy_allowed"
    BLOCKED_PRIVATE = "blocked_private"
    BLOCKED_UNRESOLVED = "blocked_unresolved"
    INVALID_URL = "invalid_url"


@dataclass(frozen=True)
class NetworkDecision:
    status: NetworkStatus
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.status == NetworkStatus.POLICY_ALLOWED


class NetworkPolicy:
    def __init__(
        self,
        *,
        allowed_cache_ttl_seconds: float = 5.0,
        blocked_cache_ttl_seconds: float = 45.0,
        max_cache_entries: int = 1024,
        allow_dns_fake_ip: bool = True,
    ) -> None:
        self.allowed_cache_ttl_seconds = max(0.0, float(allowed_cache_ttl_seconds))
        self.blocked_cache_ttl_seconds = max(0.0, float(blocked_cache_ttl_seconds))
        self.max_cache_entries = max(1, int(max_cache_entries))
        self.allow_dns_fake_ip = bool(allow_dns_fake_ip)
        self._cache: dict[tuple[str, str, int], tuple[float, str | None]] = {}
        self._lock = threading.Lock()

    def evaluate_url(self, url: str, *, resolve: bool = True, use_cache: bool = False) -> NetworkDecision:
        reason = self.check_url(url, resolve=resolve, use_cache=use_cache)
        if reason is None:
            return NetworkDecision(NetworkStatus.POLICY_ALLOWED)
        if reason in (
            "invalid URL",
            "invalid URL host",
            "invalid URL port",
            "URL has no host",
            "only http(s) URLs are allowed",
        ):
            return NetworkDecision(NetworkStatus.INVALID_URL, reason=reason)
        if reason == "could not resolve host":
            return NetworkDecision(NetworkStatus.BLOCKED_UNRESOLVED, reason=reason)
        return NetworkDecision(NetworkStatus.BLOCKED_PRIVATE, reason=reason)

    def check_url(self, url: str, *, resolve: bool = True, use_cache: bool = False) -> str | None:
        try:
            parsed = urlparse((url or "").strip())
        except ValueError:
            return "invalid URL"

        if parsed.scheme not in ("http", "https"):
            return "only http(s) URLs are allowed"

        try:
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            message = str(exc).lower()
            return "invalid URL port" if "port" in message else "invalid URL"

        if not host:
            return "URL has no host"

        normalized_host = host.lower()
        if normalized_host in _BLOCKED_HOSTS:
            return "refusing to open a local/loopback address"

        try:
            ip = ipaddress.ip_address(normalized_host)
        except ValueError:
            ip = None

        if ip is not None:
            return "refusing to open a non-public address" if _ip_is_blocked(ip) else None

        if not is_valid_hostname(normalized_host):
            return "invalid URL host"

        if not resolve:
            return None

        effective_port = port or (443 if parsed.scheme == "https" else 80)
        cache_key = (parsed.scheme, normalized_host, effective_port)

        now = time.monotonic()
        if use_cache:
            with self._lock:
                entry = self._cache.get(cache_key)
                if entry is not None:
                    cached_at, cached_reason = entry
                    ttl = self.allowed_cache_ttl_seconds if cached_reason is None else self.blocked_cache_ttl_seconds
                    if now - cached_at <= ttl:
                        return cached_reason

        try:
            infos = socket.getaddrinfo(normalized_host, effective_port, proto=socket.IPPROTO_TCP)
        except OSError:
            reason = "could not resolve host"
            if use_cache:
                self._record_cache(cache_key, reason, now)
            return reason

        saw_resolved_address = False
        reason = None
        for info in infos:
            address = info[4][0].split("%")[0]
            try:
                resolved = ipaddress.ip_address(address)
            except ValueError:
                continue
            saw_resolved_address = True
            if _ip_is_blocked(resolved) and not (self.allow_dns_fake_ip and _ip_is_dns_fake_ip(resolved)):
                reason = "refusing to open a non-public address"
                break

        if not saw_resolved_address and reason is None:
            reason = "could not resolve host"

        if use_cache:
            self._record_cache(cache_key, reason, now)

        return reason

    def _record_cache(self, key: tuple[str, str, int], reason: str | None, now: float) -> None:
        with self._lock:
            if len(self._cache) >= self.max_cache_entries:
                oldest_key = min(self._cache, key=lambda k: self._cache[k][0], default=None)
                if oldest_key is not None:
                    self._cache.pop(oldest_key, None)
            self._cache[key] = (now, reason)


DEFAULT_NETWORK_POLICY = NetworkPolicy()


def check_fetch_url(url: str, *, resolve: bool = True, use_cache: bool = False) -> str | None:
    """Evaluate ``url`` against default network policy."""
    return DEFAULT_NETWORK_POLICY.check_url(url, resolve=resolve, use_cache=use_cache)


__all__ = [
    "DEFAULT_NETWORK_POLICY",
    "NetworkDecision",
    "NetworkPolicy",
    "NetworkStatus",
    "check_fetch_url",
    "is_valid_hostname",
]
