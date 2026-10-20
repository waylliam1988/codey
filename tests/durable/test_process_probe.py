"""Checking a scheduler owner must never stop it or its console group."""

import os
import subprocess
import sys

import pytest

from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore
from codey.durable.errors import SchedulerOwnershipError
from codey.durable.process import process_is_alive


@pytest.mark.parametrize("pid", [None, True, False, 0, -1, "123"])
def test_invalid_owner_is_not_alive(pid):
    assert process_is_alive(pid) is False


def test_probe_current_and_child_process_without_signalling(monkeypatch):
    monkeypatch.setattr(os, "kill", lambda *_args: pytest.fail("Windows process probes must not send signals"))
    assert process_is_alive(os.getpid())
    if os.name != "nt":
        return
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        assert process_is_alive(child.pid)
        assert child.poll() is None
        child.terminate()
        child.wait(timeout=5)
        assert process_is_alive(child.pid) is False
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_second_scheduler_cannot_kill_the_live_owner(tmp_path, backend):
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    try:
        store.acquire_scheduler_owner("first", os.getpid())
        with pytest.raises(SchedulerOwnershipError):
            store.acquire_scheduler_owner("second", os.getpid())
        assert process_is_alive(os.getpid())
    finally:
        session.close()
