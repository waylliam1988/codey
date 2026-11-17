"""Exercise an already-created schema-seven owner from immutable Git objects."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from tests.manual.ghost_memory_routing.git_projection_delete_probe import GitGhostFinder

LEGACY_REVISION = "7244c1e3"


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload), flush=True)


def run(home: Path, store: str, operation: str) -> None:
    finder = GitGhostFinder(LEGACY_REVISION)
    sys.meta_path.insert(0, finder)
    try:
        from codey.ghost.graph_store import GhostGraph
        from codey.ghost.observations import MAX_OBSERVATIONS
        from tests.test_ghost_capacity_history_retention import _owner
        from tests.test_ghost_scope_history_retention import NOW, STORES, _history, _seed_history, _visible

        if store not in STORES or operation not in {"compact", "reset"}:
            raise ValueError("unknown legacy store or operation")
        surface = _seed_history(home, "project")
        assert surface.observations is not None
        if store == "observations" and operation == "compact":
            with surface.observations.database.write_transaction() as database:
                database.executemany(
                    "INSERT INTO observation_events(run_id,session_id,project,mode,user_text,assistant_text,"
                    "stop_reason,committed,provider_id,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        (f"legacy-capacity-{index:06d}", "session", str(home / "project"), "chat", "Capacity fact.",
                         "Recorded.", "done", 1, "fixture", NOW)
                        for index in range(MAX_OBSERVATIONS)
                    ),
                )
        assert _history(surface, store) and _visible(surface, store)
        with surface.observations.database.connection() as database:
            version: sqlite3.Row | None = database.execute("PRAGMA user_version").fetchone()
            assert version is not None
            number: object = version[0]
            assert number == 7
        _emit({"ready": True, "schema": 7, "revision": finder.revision})
        if input() != "go":
            raise ValueError("expected explicit release")
        try:
            if store == "observations":
                if operation == "reset":
                    surface.observations.reset_all()
                elif not surface.observations.append_completed(
                    run_id="legacy-fresh-run", session_id="session", mode="chat", user_text="New fact",
                    assistant_text="Recorded.", stop_reason="done",
                ):
                    _emit({"outcome": "rejected", "error": "append_returned_false"})
                    return
            else:
                owner = _owner(surface, store)
                if operation == "reset":
                    owner.reset()
                else:
                    with owner.transaction():
                        events = owner.events()
                        if isinstance(owner, GhostGraph):
                            nodes, edges = owner.rows()
                            owner.append(events, replace=True)
                            owner.replace_rows(nodes, edges)
                        else:
                            rows = owner.rows()
                            owner.append(events, replace=True)
                            owner.replace_rows(rows)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _emit({"outcome": "rejected", "error": type(exc).__name__})
        else:
            _emit({"outcome": "completed", "error": ""})
    finally:
        sys.meta_path.remove(finder)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--operation", required=True)
    arguments = parser.parse_args()
    home: object = arguments.home
    store: object = arguments.store
    operation: object = arguments.operation
    if not isinstance(home, Path) or not isinstance(store, str) or not isinstance(operation, str):
        raise TypeError("invalid worker arguments")
    run(home, store, operation)


if __name__ == "__main__":
    main()
