"""A completed Future may invoke its callback immediately on the submitting thread."""

import threading
from collections.abc import Callable
from concurrent.futures import Future

import pytest

from codey.app.connection_status import ConnectionSnapshot, ConnectionStatusCoordinator, ProbeRequest


def test_status_notification_does_not_hold_the_coordinator_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    class Connector:
        def probe_status(self, request: ProbeRequest) -> dict[str, object]:
            return {"state": "ready"}

    readers: list[threading.Thread] = []
    reads_completed_during_notification: list[bool] = []

    def emit(event: dict[str, object]) -> None:
        finished = threading.Event()

        def read_snapshot() -> None:
            assert coordinator.current("fixture").state == "ready"
            finished.set()

        reader = threading.Thread(target=read_snapshot)
        readers.append(reader)
        reader.start()
        reads_completed_during_notification.append(finished.wait(5))

    coordinator = ConnectionStatusCoordinator(
        provider_ids=lambda: ("fixture",), labels=lambda: {"fixture": "Fixture"},
        enabled=lambda _: True, connector=lambda _: Connector(), emit=emit,
    )

    def completed_submit(
        operation: Callable[[ProbeRequest], ConnectionSnapshot], request: ProbeRequest,
    ) -> Future[ConnectionSnapshot]:
        future: Future[ConnectionSnapshot] = Future()
        future.set_result(operation(request))
        return future

    monkeypatch.setattr(coordinator._executor, "submit", completed_submit)
    try:
        coordinator.refresh("fixture")
    finally:
        for reader in readers:
            reader.join(timeout=5)
        coordinator.close()

    assert all(not reader.is_alive() for reader in readers)
    assert reads_completed_during_notification == [True]
