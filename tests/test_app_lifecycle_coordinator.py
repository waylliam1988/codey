from __future__ import annotations

import threading

from codey.app.lifecycle import AppLifecycle, ResourceSlot


def test_shutdown_rejects_admission_and_drains_shell_before_releasing_resources() -> None:
    events: list[str] = []
    draining = threading.Event()

    def workers() -> bool:
        events.append("workers")
        draining.set()
        return True

    def step(name: str) -> bool:
        events.append(name)
        return True

    lifecycle = AppLifecycle(
        stop=lambda: events.append("stop"), close_connections=lambda: events.append("connections"),
        close_conversations=lambda: step("conversations"), stop_scheduler=lambda: step("scheduler"),
        wait_workers=workers, close_controls=lambda: step("controls"), cleanup=(lambda: step("store"),),
    )
    assert lifecycle.begin_shell_execution()
    result: list[bool] = []
    closer = threading.Thread(target=lambda: result.append(lifecycle.close()))
    closer.start()
    try:
        assert draining.wait(2)
        assert lifecycle.close_requested
        assert not lifecycle.begin_shell_execution()
        assert not lifecycle.resources_closed
        assert "store" not in events
    finally:
        lifecycle.end_shell_execution()
        closer.join(3)
    assert not closer.is_alive()
    assert result == [True]
    assert events == ["stop", "connections", "conversations", "scheduler", "workers", "controls", "store"]
    assert lifecycle.close()
    assert events.count("store") == 1


def test_failed_resource_stays_attached_until_cleanup_succeeds() -> None:
    resource = object()
    owned: list[object] = [resource]
    attempts = 0

    def release(value: object) -> None:
        nonlocal attempts
        assert value is resource
        attempts += 1
        if attempts == 1:
            raise OSError("resource is busy")

    slot = ResourceSlot(lambda: owned[0] if owned else None, release, lambda _value: owned.clear(), attempts=1)
    assert not slot.close()
    assert owned == [resource]
    assert slot.close()
    assert not owned
    assert slot.close()
    assert attempts == 2
