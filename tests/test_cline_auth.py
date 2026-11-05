"""Account races, storage atomicity and redaction through real OAuth control code."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.providers.cline import auth as auth_module
from codey.providers.cline.auth import Auth
from codey.providers.cline.credentials import WindowsTokenStore
from codey.providers.cline.http import JsonReply
from codey.providers.cline.identity import inference_api_key
from codey.providers.connection_controls import ConnectionActionError
from tests.support.cline import MemoryStore, ScriptHTTP, credential, token_response


@pytest.mark.parametrize("raw,expected", [(" raw ", "workos:raw"), ("workos:raw", "workos:raw")])
def test_inference_prefix_is_not_a_storage_mutation(raw, expected):
    record = replace(credential(), access_token=raw)
    assert inference_api_key(record.access_token) == expected
    assert record.access_token == raw and raw not in repr(record)


@pytest.mark.parametrize("raw", ["", " ", "workos:"])
def test_empty_inference_key_is_rejected(raw):
    with pytest.raises(ValueError):
        inference_api_key(raw)


def synchronous_login(tmp_path, monkeypatch, *, hook=None, browser=lambda _: True):
    ticks = [0.0]
    waits = []
    event = threading.Event()

    class ClockEvent:
        def is_set(self):
            return event.is_set()

        def set(self):
            event.set()

        def wait(self, seconds):
            waits.append(seconds)
            ticks[0] += seconds
            return event.is_set()

    monkeypatch.setattr(auth_module, "time", SimpleNamespace(monotonic=lambda: ticks[0]))
    http, store = ScriptHTTP(), MemoryStore()
    http.hook = hook
    auth = Auth(store, http, tmp_path / "credentials", browser=browser)
    auth.attempt, auth.state = "attempt", "starting"
    auth._cancel = ClockEvent()
    return auth, http, store, waits


def test_device_pending_slowdown_and_exchange_are_atomic(tmp_path, monkeypatch):
    replies = iter([JsonReply(400, {"error": "authorization_pending"}), JsonReply(400, {"error": "slow_down"}),
                    JsonReply(200, {"access_token": "WORKOS_ACCESS", "refresh_token": "WORKOS_REFRESH"})])
    opened = []
    auth, http, store, waits = synchronous_login(tmp_path, monkeypatch,
        hook=lambda url, *_: next(replies) if url.endswith("/authenticate") else None,
        browser=lambda url: opened.append(url))
    auth._login(0, auth._cancel)
    assert waits == [1, 1, 6]
    assert len(opened) == 1 and len(store.saved) == 1
    snapshot = auth.snapshot()
    assert snapshot["state"] == "ready" and snapshot["user_code"] is None
    assert store.load().account_epoch and store.load().token_version == 0
    for marker in ("ACCESS", "REFRESH", "DEVICE_MARKER", "USER_CODE_MARKER"):
        assert marker not in json.dumps(snapshot)
    register = next(call for call in http.calls if call[0].endswith("/register"))
    assert register[1] == {"accessToken": "WORKOS_ACCESS", "refreshToken": "WORKOS_REFRESH"}


def test_cancel_during_registration_rejects_late_success(tmp_path, monkeypatch):
    auth, http, store, _ = synchronous_login(tmp_path, monkeypatch)

    def hook(url, *_):
        if url.endswith("/register"):
            auth.cancel_login("attempt")
        return None

    http.hook = hook
    auth._login(0, auth._cancel)
    assert auth.snapshot()["state"] == "cancelled" and not store.saved and store.load() is None


def test_old_attempt_cannot_replace_new_attempt(tmp_path, monkeypatch):
    auth, http, store, _ = synchronous_login(tmp_path, monkeypatch)

    def hook(url, *_):
        if url.endswith("/register"):
            auth._generation += 1
            auth.attempt = "new-attempt"
            auth._state("starting")

    http.hook = hook
    auth._login(0, auth._cancel)
    assert auth.attempt == "new-attempt" and auth.state == "starting" and not store.saved


@pytest.mark.parametrize("error", ["expired_token", "access_denied", "invalid_grant"])
def test_device_rejections_stop_and_clear_one_time_codes(tmp_path, monkeypatch, error):
    auth, http, store, _ = synchronous_login(tmp_path, monkeypatch,
        hook=lambda url, *_: JsonReply(400, {"error": error}) if url.endswith("/authenticate") else None)
    auth._login(0, auth._cancel)
    assert auth.state == ("expired" if error == "expired_token" else "error") and auth.user_code is None and not store.saved


def test_device_browser_origin_is_validated(tmp_path, monkeypatch):
    auth, http, store, _ = synchronous_login(tmp_path, monkeypatch)
    http.hook = lambda url, *_: JsonReply(200, {"device_code": "d", "user_code": "u", "expires_in": 300,
        "interval": 1, "verification_uri": "https://evil.example/device"}) if url.endswith("/device") else None
    auth._login(0, auth._cancel)
    assert auth.state == "error" and len(http.calls) == 1 and not store.saved


def test_duplicate_login_reuses_attempt_and_close_preserves_credentials(tmp_path):
    started, release = threading.Event(), threading.Event()
    http, store = ScriptHTTP(), MemoryStore()

    def hook(url, *_):
        if url.endswith("/device"):
            started.set()
            assert release.wait(3)

    http.hook = hook
    auth = Auth(store, http, tmp_path / "credentials", browser=lambda _: True)
    first = auth.start_login()
    assert started.wait(2)
    assert auth.start_login()["attempt_id"] == first["attempt_id"]
    auth.close()
    release.set()
    auth._thread.join(3)
    assert not store.saved
    store.save(credential())
    auth.close()
    assert store.load() is not None


def test_ten_simultaneous_refreshes_rotate_only_once(tmp_path):
    http, store = ScriptHTTP(), MemoryStore(credential(expires=1))
    auth = Auth(store, http, tmp_path / "credentials")
    barrier = threading.Barrier(10)

    def obtain():
        barrier.wait()
        return auth.credentials(time.monotonic() + 5, lambda: False, "account-A")

    with ThreadPoolExecutor(max_workers=10) as pool:
        records = list(pool.map(lambda _: obtain(), range(10)))
    assert len(http.calls) == 1 and len(store.saved) == 1
    assert {record.token_version for record in records} == {1}
    assert {record.access_token for record in records} == {"ACCESS_ROTATED"}


@pytest.mark.parametrize("status,state", [(503, "signed_out"), (401, "reauth_required")])
def test_refresh_rejection_and_network_failure_have_distinct_states(tmp_path, status, state):
    http, store = ScriptHTTP(), MemoryStore(credential(expires=1))
    original = store.load()
    http.hook = lambda *_: JsonReply(status, {"error": "SECRET_MARKER"})
    auth = Auth(store, http, tmp_path / "credentials")
    with pytest.raises(RuntimeError) as error:
        auth.credentials(time.monotonic() + 2, lambda: False, "account-A")
    assert "SECRET_MARKER" not in str(error.value)
    assert auth.state == state and store.load() is original
    assert auth.snapshot()["state"] == ("ready" if status == 503 else "reauth_required")


def test_refresh_cancellation_and_account_change_never_write(tmp_path):
    http, store = ScriptHTTP(), MemoryStore(credential(expires=1))
    auth = Auth(store, http, tmp_path / "credentials")
    event = threading.Event()
    http.hook = lambda *_: event.set() or JsonReply(200, token_response())
    with pytest.raises(TimeoutError):
        auth.credentials(time.monotonic() + 2, event.is_set, "account-A")
    assert not store.saved
    with pytest.raises(ConnectionActionError):
        auth.credentials(time.monotonic() + 2, lambda: False, "account-B")


def test_secure_storage_failure_does_not_fall_back_to_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr("codey.providers.cline.credentials.sys.platform", "linux")
    store = WindowsTokenStore("Codey/Cline/test-only")
    with pytest.raises(ConnectionActionError, match="credential store"):
        store.save(credential())
    assert list(tmp_path.iterdir()) == []


def test_late_browser_failure_cannot_change_replacement_login(tmp_path):
    auth = Auth(MemoryStore(), ScriptHTTP(), tmp_path / "credentials")
    auth.state, auth.attempt, auth._generation = "awaiting_authorization", "old", 1

    def browser(_):
        auth.cancel_login("old")
        auth.attempt = "new"
        auth._state("starting")
        return False

    auth.browser = browser
    auth._open_browser("https://authkit.cline.bot/device", 1)
    assert auth.attempt == "new" and auth.state == "starting" and auth.error_code is None


def test_external_account_changes_advance_public_revision_without_exposing_epoch(tmp_path):
    store = MemoryStore(credential())
    auth = Auth(store, ScriptHTTP(), tmp_path / "credentials")
    first = auth.snapshot()
    assert auth.snapshot() == first
    auth._state("reauth_required", "reauth_required")
    store.save(credential(epoch="PRIVATE_ACCOUNT_B"))
    second = auth.snapshot()
    assert second["state"] == "ready" and second["revision"] > first["revision"]
    assert second["error_code"] is None
    assert "PRIVATE_ACCOUNT_B" not in json.dumps(second)
    store.delete()
    third = auth.snapshot()
    assert third["state"] == "signed_out" and third["revision"] > second["revision"]


def test_backend_restart_advances_auth_revision_for_an_already_open_settings_page(tmp_path, monkeypatch):
    tick = auth_module.time_ns()
    monkeypatch.setattr(auth_module, "time_ns", lambda: tick)
    store = MemoryStore(credential())
    auth = Auth(store, ScriptHTTP(), tmp_path / "credentials")
    connected = auth.snapshot()
    auth.close()
    restarted = Auth(store, ScriptHTTP(), tmp_path / "credentials")
    restarted.logout()
    signed_out = restarted.snapshot()
    assert signed_out["state"] == "signed_out"
    assert connected["revision"] < signed_out["revision"] < 2 ** 53


def test_new_storage_error_advances_revision_and_recovery_clears_the_error(tmp_path, monkeypatch):
    store = MemoryStore(credential())
    auth = Auth(store, ScriptHTTP(), tmp_path / "credentials")
    connected = auth.snapshot()
    load = store.load

    def fail():
        raise ConnectionActionError("auth_storage_failed", "PRIVATE_FAILURE", 503)

    monkeypatch.setattr(store, "load", fail)
    failed = auth.snapshot()
    assert failed["state"] == "error" and failed["revision"] > connected["revision"]
    assert auth.snapshot() == failed and "PRIVATE_FAILURE" not in json.dumps(failed)
    monkeypatch.setattr(store, "load", load)
    recovered = auth.snapshot()
    assert recovered["state"] == "ready" and recovered["error_code"] is None
    assert recovered["revision"] > failed["revision"]
