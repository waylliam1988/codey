"""Process-local operator credentials; never task or model capabilities."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from http.cookies import CookieError, SimpleCookie

BOOTSTRAP_LIFETIME_SECONDS = 300.0


class OperatorAuth:
    def __init__(self, port: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.cookie_name = f"codey_operator_{port}"
        self._session_token = secrets.token_urlsafe(32)
        self._bootstrap_token = ""
        self._bootstrap_deadline = 0.0
        self._clock = clock
        self._lock = threading.Lock()

    def issue_bootstrap(self) -> str:
        with self._lock:
            self._bootstrap_token = secrets.token_urlsafe(32)
            self._bootstrap_deadline = self._clock() + BOOTSTRAP_LIFETIME_SECONDS
            return self._bootstrap_token

    def exchange(self, token: object) -> str | None:
        if type(token) is not str or not token.isascii():
            return None
        with self._lock:
            if (not self._bootstrap_token or self._clock() >= self._bootstrap_deadline
                    or not secrets.compare_digest(token, self._bootstrap_token)):
                return None
            self._bootstrap_token = ""
            return f"{self.cookie_name}={self._session_token}; Path=/; HttpOnly; SameSite=Strict"

    def authenticated(self, cookie_header: str | None) -> bool:
        if not cookie_header:
            return False
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except CookieError:
            return False
        morsel = cookie.get(self.cookie_name)
        return morsel is not None and morsel.value.isascii() and secrets.compare_digest(morsel.value, self._session_token)
