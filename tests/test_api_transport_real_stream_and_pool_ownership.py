"""Deterministic connection-lease ownership regressions."""

import threading

import pytest

from codey.providers.api_http import ApiHttpSession, ConnectionKey
from codey.providers.api_transport import _PooledResponse, abort_response

KEY = ConnectionKey("http://fixture", "", "", "http")


class _Connection:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class _Response:
    status = 200
    reason = "OK"
    headers = {"Content-Length": "0"}
    msg = headers

    def __init__(self) -> None:
        self.closed = 0

    def read(self, _size: int = -1) -> bytes:
        return b""

    def close(self) -> None:
        self.closed += 1

    def isclosed(self) -> bool:
        return True


class _BlockingResponse(_Response):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = threading.Event()
        self.allow_close = threading.Event()

    def close(self) -> None:
        self.close_started.set()
        self.allow_close.wait(1)
        super().close()


class _SocketConnection(_Connection):
    def __init__(self) -> None:
        super().__init__()
        self.shutdown_called = 0

    def shutdown(self, *_args) -> None:
        self.shutdown_called += 1


def test_old_response_abort_cannot_shutdown_reborrowed_socket() -> None:
    session = ApiHttpSession()
    connection = _Connection()
    session.release_connection(KEY, connection)
    lease_a = session.acquire_lease(KEY)
    assert lease_a is not None
    response_a = _PooledResponse(_Response(), lease_a)
    response_a.read()
    response_a.close()

    lease_b = session.acquire_lease(KEY)
    assert lease_b is not None
    assert lease_b.connection is connection

    abort_response(response_a)
    assert connection.closed == 0

    connection_b = lease_b.cancel()
    assert connection_b is connection
    session.discard_connection(connection_b)
    session.release()


def test_late_return_after_last_release_closes_connection() -> None:
    session = ApiHttpSession()
    connection = _Connection()
    session.release_connection(KEY, connection)
    lease = session.acquire_lease(KEY)
    assert lease is not None
    session.release()
    owned = lease.begin_return()
    assert owned is connection
    lease.finish_return(owned, True)
    assert connection.closed == 1
    with pytest.raises(RuntimeError, match="closed"):
        session.retain()


def test_closed_session_rejects_acquire_and_destroys_late_return() -> None:
    session = ApiHttpSession()
    session.release()
    with pytest.raises(RuntimeError, match="closed"):
        session.acquire_lease(KEY)
    connection = _Connection()
    session.release_connection(KEY, connection)
    assert connection.closed == 1


def test_cancel_wins_before_return_discards_connection_once() -> None:
    session = ApiHttpSession()
    connection = _Connection()
    lease = session.acquire_lease(KEY)
    assert lease is None
    session.release_connection(KEY, connection)
    lease = session.acquire_lease(KEY)
    assert lease is not None
    owned = lease.cancel()
    assert owned is connection
    session.discard_connection(owned)
    assert connection.closed == 1
    assert lease.begin_return() is None


def test_pooled_abort_shuts_socket_before_blocking_response_close() -> None:
    session = ApiHttpSession()
    connection = _SocketConnection()
    session.release_connection(KEY, connection)
    lease = session.acquire_lease(KEY)
    assert lease is not None
    response = _BlockingResponse()
    pooled = _PooledResponse(response, lease)
    thread = threading.Thread(target=pooled.abort)
    thread.start()
    assert response.close_started.wait(1)
    assert connection.shutdown_called >= 1
    response.allow_close.set()
    thread.join(1)
    assert not thread.is_alive()

