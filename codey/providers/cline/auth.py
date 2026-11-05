"""Device OAuth and account-bound token rotation; secrets never leave this layer."""
from __future__ import annotations

import contextlib
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable, Generator
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from codey.providers.cline.credentials import CredentialRecord, TokenStore
from codey.providers.cline.http import JsonClient
from codey.providers.cline.identity import API_ORIGIN, WORKOS_CLIENT_ID, WORKOS_ORIGIN
from codey.providers.connection_controls import ConnectionActionError
from codey.storage.file_lock import LockTimeout, with_file_lock

_ACTIVE = {"starting", "awaiting_authorization", "exchanging"}


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 16000:
        raise ValueError("invalid authorization response")
    return value


class Auth:
    def __init__(self, store: TokenStore, http: JsonClient, lock_path: Path, *,
                 browser: Callable[[str], object] = webbrowser.open,
                 clock: Callable[[], float] = time.time) -> None:
        self.store, self.http, self.lock_path = store, http, lock_path
        self.browser, self.clock = browser, clock
        self.lock = threading.RLock()
        self.state = "signed_out"
        self.revision = 0
        self.attempt: str | None = None
        self.expires_at: float | None = None
        self.user_code: str | None = None
        self.error_code: str | None = None
        self._url = ""
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._observed_epoch: str | None = None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            try:
                record = self.store.load()
                epoch = record.account_epoch if record is not None else None
                previous = self.state
                if epoch != self._observed_epoch:
                    previous_epoch = self._observed_epoch
                    self._observed_epoch = epoch
                    self.revision += 1
                    if self.state == "reauth_required" and record is not None and previous_epoch is not None:
                        self.state = "ready"
                if self.state not in _ACTIVE | {"reauth_required"}:
                    self.state = "ready" if record is not None else (self.state if self.state != "ready" else "signed_out")
                if previous != self.state:
                    if self.state in {"ready", "signed_out"}:
                        self.error_code = None
                    self.revision += 1
            except ConnectionActionError:
                self.state, self.error_code = "error", "auth_storage_failed"
            actions = (["logout", "clear_data"] if self.state == "ready" else
                       ["cancel_login", "reopen_login"] if self.state == "awaiting_authorization" else
                       ["cancel_login"] if self.state in _ACTIVE else ["login", "clear_data"])
            return {"kind": "oauth_device", "state": self.state, "revision": self.revision,
                    "attempt_id": self.attempt, "expires_at": self.expires_at, "user_code": self.user_code,
                    "error_code": self.error_code, "actions": actions}

    def _state(self, state: str, error: str | None = None) -> None:
        self.state, self.error_code = state, error
        self.revision += 1
        if state not in _ACTIVE:
            self.user_code, self.expires_at, self._url = None, None, ""

    def start_login(self) -> dict[str, Any]:
        with self.lock:
            if self.state in _ACTIVE:
                return self.snapshot()
            if self.store.load() is not None and self.state != "reauth_required":
                return self.snapshot()
            self._generation += 1
            self.attempt = uuid4().hex
            self._cancel = threading.Event()
            self._state("starting")
            self._thread = threading.Thread(target=self._login, args=(self._generation, self._cancel),
                                            name="cline-device-login", daemon=True)
            self._thread.start()
            return self.snapshot()

    def _current(self, generation: int, cancel: threading.Event) -> bool:
        return self._generation == generation and not cancel.is_set()

    def _login(self, generation: int, cancel: threading.Event) -> None:
        try:
            device = self.http.request(WORKOS_ORIGIN + "/user_management/authorize/device",
                {"client_id": WORKOS_CLIENT_ID}, form=True, deadline=time.monotonic() + 30, cancelled=cancel.is_set)
            if device.status != 200:
                raise ValueError("device authorization rejected")
            data = device.body
            device_code, user_code = _text(data.get("device_code")), _text(data.get("user_code"))
            url = _text(data.get("verification_uri_complete") or data.get("verification_uri"))
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname not in {"authkit.cline.bot", "auth.cline.bot"} or parsed.username or parsed.port:
                raise ValueError("unexpected authorization origin")
            expires, interval = data.get("expires_in"), data.get("interval")
            if (type(expires) is not int or not 1 <= expires <= 1800
                    or type(interval) is not int or not 1 <= interval <= 60):
                raise ValueError("invalid authorization deadline")
            deadline = time.monotonic() + expires
            with self.lock:
                if not self._current(generation, cancel):
                    return
                self.user_code, self._url, self.expires_at = user_code, url, self.clock() + expires
                self._state("awaiting_authorization")
            if not cancel.is_set():
                self._open_browser(url, generation)
            tokens = self._poll(device_code, deadline, interval, cancel)
            with self.lock:
                if not self._current(generation, cancel):
                    return
                self._state("exchanging")
            reply = self.http.request(API_ORIGIN + "/api/v1/auth/register", {
                "accessToken": _text(tokens.get("access_token")), "refreshToken": _text(tokens.get("refresh_token"))},
                deadline=deadline, cancelled=cancel.is_set)
            record = self._record(reply.body, uuid4().hex, 0)
            if reply.status != 200:
                raise ValueError("token registration rejected")
            with self.transaction(deadline, cancel.is_set), self.lock:
                if not self._current(generation, cancel):
                    return
                self.store.save(record)
                self._state("ready")
        except Exception as exc:
            with self.lock:
                if self._current(generation, cancel):
                    code = exc.code if isinstance(exc, ConnectionActionError) else "authorization_failed"
                    self._state("expired" if isinstance(exc, TimeoutError) else "error", code)

    def _poll(self, code: str, deadline: float, interval: int, cancel: threading.Event) -> dict[str, Any]:
        failures = 0
        while time.monotonic() < deadline:
            if cancel.wait(min(interval, max(0, deadline - time.monotonic()))):
                raise TimeoutError("authorization cancelled")
            if time.monotonic() >= deadline:
                break
            try:
                reply = self.http.request(WORKOS_ORIGIN + "/user_management/authenticate", {
                    "client_id": WORKOS_CLIENT_ID, "device_code": code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
                    form=True, deadline=deadline, cancelled=cancel.is_set)
            except RuntimeError:
                failures += 1
                if failures >= 3:
                    raise
                continue
            if reply.status == 200:
                return reply.body
            error = reply.body.get("error")
            if error == "slow_down":
                interval += 5
            elif error == "expired_token":
                raise TimeoutError("authorization expired")
            elif error != "authorization_pending":
                raise ValueError("authorization rejected")
        raise TimeoutError("authorization expired")

    def _open_browser(self, url: str, generation: int) -> None:
        try:
            opened = self.browser(url)
        except Exception:
            opened = False
        if opened is False:
            with self.lock:
                if generation == self._generation and self.state == "awaiting_authorization":
                    self.error_code = "browser_open_failed"
                    self.revision += 1

    def cancel_login(self, attempt: str | None) -> dict[str, Any]:
        with self.lock:
            if not attempt or attempt != self.attempt:
                raise ConnectionActionError("stale_attempt", "This login attempt is no longer current.", 409)
            self._cancel.set()
            self._generation += 1
            self._state("cancelled")
            return self.snapshot()

    def reopen_login(self, attempt: str | None) -> dict[str, Any]:
        with self.lock:
            if attempt != self.attempt or self.state != "awaiting_authorization":
                raise ConnectionActionError("stale_attempt", "This login attempt is no longer current.", 409)
            url = self._url
            generation = self._generation
        self._open_browser(url, generation)
        return self.snapshot()

    @contextlib.contextmanager
    def transaction(self, deadline: float, cancelled: Callable[[], bool]) -> Generator[None, None, None]:
        while True:
            if cancelled() or time.monotonic() >= deadline:
                raise TimeoutError("credential operation cancelled or timed out")
            try:
                with with_file_lock(self.lock_path, timeout_seconds=0.05):
                    yield
                    return
            except LockTimeout:
                continue

    def _record(self, body: dict[str, Any], epoch: str, version: int, fallback: str = "") -> CredentialRecord:
        data = body.get("data")
        if body.get("success") is not True or not isinstance(data, dict):
            raise ValueError("invalid token response")
        expires = data.get("expiresAt")
        if not isinstance(expires, str):
            raise ValueError("invalid token expiry")
        instant = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.timestamp() <= self.clock():
            raise ValueError("invalid token expiry")
        return CredentialRecord(_text(data.get("accessToken")), _text(data.get("refreshToken") or fallback),
                                instant.timestamp(), epoch, version)

    def credentials(self, deadline: float, cancelled: Callable[[], bool], expected_epoch: str, *,
                    force_version: int | None = None) -> CredentialRecord:
        initial = self.store.load()
        self._check_record(initial, expected_epoch)
        assert initial is not None
        if force_version is None and initial.expires_at - self.clock() > 120:
            if cancelled() or time.monotonic() >= deadline:
                raise TimeoutError("credential operation cancelled or timed out")
            return initial
        with self.transaction(deadline, cancelled):
            record = self.store.load()
            self._check_record(record, expected_epoch)
            assert record is not None
            if record.token_version != initial.token_version or (force_version is not None and record.token_version != force_version):
                return record
            if force_version is None and record.expires_at - self.clock() > 120:
                return record
            with self.lock:
                generation = self._generation
            reply = self.http.request(API_ORIGIN + "/api/v1/auth/refresh", {
                "refreshToken": record.refresh_token, "grantType": "refresh_token"}, deadline=deadline, cancelled=cancelled)
            if reply.status in {400, 401, 403}:
                with self.lock:
                    self._state("reauth_required", "reauth_required")
                raise ConnectionActionError("reauth_required", "Sign in to Cline again.", 401)
            if reply.status != 200:
                raise RuntimeError("Cline token refresh failed; retry.")
            updated = self._record(reply.body, record.account_epoch, record.token_version + 1, record.refresh_token)
            with self.lock:
                if cancelled() or time.monotonic() >= deadline or generation != self._generation:
                    raise TimeoutError("credential refresh cancelled")
                self.store.save(updated)
                self._state("ready")
            return updated

    def _check_record(self, record: CredentialRecord | None, epoch: str) -> None:
        if record is None or record.account_epoch != epoch or self.state in _ACTIVE | {"reauth_required"}:
            raise ConnectionActionError("reauth_required", "Cline account changed or requires sign-in; select the model again.", 401)

    def logout(self) -> None:
        with self.lock:
            self._generation += 1
            self._cancel.set()
        with self.transaction(time.monotonic() + 2, lambda: False), self.lock:
            self.store.delete()
            self.attempt = None
            self._state("signed_out")

    def close(self) -> None:
        with self.lock:
            self._generation += 1
            self._cancel.set()
            if self.state in _ACTIVE:
                self._state("cancelled")
