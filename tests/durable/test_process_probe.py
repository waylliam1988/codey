"""Checking a scheduler owner must never stop it or its console group."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.errors import SchedulerOwnershipError
from codey.durable.process import process_is_alive
from tests.support.sqlite_store import TemporarySQLiteStore


@pytest.mark.parametrize("pid", [None, True, False, 0, -1, "123"])
def test_invalid_owner_is_not_alive(pid: object) -> None:
    assert process_is_alive(pid) is False


def test_probe_current_and_child_process_without_signalling(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_signal(_pid: int, _signal: int) -> None:
        pytest.fail("Windows process probes must not send signals")

    monkeypatch.setattr(os, "kill", unexpected_signal)
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


@pytest.mark.skipif(os.name != "nt", reason="checks a Windows process exit status")
def test_exited_process_with_still_active_exit_code_is_not_alive() -> None:
    with subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(259)"]) as child:
        assert child.wait(timeout=5) == 259
        assert not process_is_alive(child.pid)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_second_scheduler_cannot_kill_the_live_owner(tmp_path: Path, backend: str) -> None:
    store = TemporarySQLiteStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    try:
        store.acquire_scheduler_owner("first", os.getpid())
        with pytest.raises(SchedulerOwnershipError):
            store.acquire_scheduler_owner("second", os.getpid())
        assert process_is_alive(os.getpid())
    finally:
        session.close()
