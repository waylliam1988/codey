"""Bounded concurrent WAL soak with deterministic failure recovery."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from codey.durable import CommitConflict, DurableSession, SQLiteDurableStore


def test_sqlite_wal_converges_under_concurrent_writers_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    seed = DurableSession(SQLiteDurableStore(path))
    seed.create_conversation("chat")
    seed.close()

    workers = 4
    operations = 25

    def worker(worker_id: int) -> int:
        session = DurableSession(SQLiteDurableStore(path))
        completed = 0
        try:
            for index in range(operations):
                for _attempt in range(20):
                    try:
                        session.append_message(
                            "chat",
                            "user_message",
                            {"worker": worker_id, "index": index},
                        )
                    except CommitConflict:
                        continue
                    completed += 1
                    break
                else:
                    raise AssertionError("concurrent append did not converge")
            return completed
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        completed = list(pool.map(worker, range(workers)))
    assert completed == [operations] * workers

    reopened = DurableSession(SQLiteDurableStore(path))
    entries = reopened.entries("chat")
    assert len(entries) == workers * operations
    sequences = [record.sequence for record in reopened.commits_after(0)]
    assert sequences == list(range(1, len(sequences) + 1))

    original_commit = reopened.store.commit
    failed = True

    def fail_once(mutation: object) -> object:
        nonlocal failed
        if failed:
            failed = False
            raise OSError("injected sqlite commit failure")
        return original_commit(mutation)  # type: ignore[arg-type]

    reopened.store.commit = fail_once  # type: ignore[method-assign]
    try:
        reopened.append_message("chat", "user_message", {"chaos": True})
    except OSError as exc:
        assert str(exc) == "injected sqlite commit failure"
    else:
        raise AssertionError("injected commit failure should propagate")
    finally:
        reopened.close()
    recovered = DurableSession(SQLiteDurableStore(path))
    try:
        assert len(recovered.entries("chat")) == workers * operations
    finally:
        recovered.close()
