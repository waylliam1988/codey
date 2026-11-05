"""Request-scoped VPN-first routing; only explicit safe failures can fall back."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from codey.providers.api_request_policy import RequestContext
from codey.providers.api_transport import GenerationNotSentError, GenerationRejectedError
from codey.providers.zen.direct import DirectTunnel, PhysicalInterface, discover_interface


@dataclass
class Route:
    direct: bool = False
    tunnel: DirectTunnel | None = None


class ZenRoutes:
    def __init__(self) -> None:
        self._routes: dict[str, Route] = {}
        self._lock = threading.RLock()
        self._interface: PhysicalInterface | None = None
        self._interface_checked = 0.0

    def register(self, context: RequestContext) -> None:
        with self._lock:
            self._routes[context.logical_id] = Route()

    def fallback(self, context: RequestContext, failure: BaseException) -> bool:
        if os.name != "nt" or context.cancelled() or time.monotonic() >= context.deadline:
            return False
        rejected = (isinstance(failure, GenerationRejectedError) and failure.status == 429
                    and failure.error_type == "FreeUsageLimitError")
        not_sent = (isinstance(failure, GenerationNotSentError)
                    and failure.reason_code == "pre_submission_network_error")
        with self._lock:
            route = self._routes.get(context.logical_id)
            if route is None or route.direct or not (rejected or not_sent):
                return False
            route.direct = True
            return True

    def transport_options(self, context: RequestContext) -> dict[str, object]:
        with self._lock:
            route = self._routes[context.logical_id]
            if not route.direct:
                return {}
            if route.tunnel is None:
                host = urlsplit(context.base_url).hostname or ""
                if self._interface is None or time.monotonic() - self._interface_checked >= 60:
                    self._interface = discover_interface(context.deadline, context.cancelled)
                    self._interface_checked = time.monotonic()
                route.tunnel = DirectTunnel(host, context.deadline, context.cancelled, interface=self._interface)
            return {"fetch_proxy": route.tunnel.proxy_url}

    def close(self, context: RequestContext) -> None:
        with self._lock:
            route = self._routes.pop(context.logical_id, None)
        if route is not None and route.tunnel is not None:
            route.tunnel.close()
