"""Measure real schema-seven retrieval after a concurrent schema-eight upgrade."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from tests.manual.ghost_memory_routing.git_projection_delete_probe import GitGhostFinder

LEGACY_REVISION = "7244c1e3"
ARCHIVED_QUERY = "archivedquartz"
ACTIVE_QUERY = "activeopal"


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload), flush=True)


def run(home: Path, phase: str, scope: str) -> None:
    finder = GitGhostFinder(LEGACY_REVISION)
    sys.meta_path.insert(0, finder)
    try:
        from codey.ghost.observation_index import render_retrieved_block
        from codey.ghost.observations import MAX_OBSERVATIONS, GhostObservationStore

        store = GhostObservationStore(home)
        project = str(home / "project")
        assert store.vector_memory is None
        assert store.append_completed(
            run_id="archived-run", session_id="old-session", project=project,
            mode="chat", user_text=ARCHIVED_QUERY, assistant_text="Recorded.", stop_reason="done",
        )
        assert store.retrieve(ARCHIVED_QUERY, project=project, session_id="old-session", scope=scope)
        with store.database.connection() as database:
            version: sqlite3.Row | None = database.execute("PRAGMA user_version").fetchone()
            assert version is not None
            number: object = version[0]
            assert number == 7
        _emit({"ready": True, "schema": 7, "revision": finder.revision})
        if input() != "go":
            raise ValueError("expected explicit release")
        if phase == "schema7_trimmed":
            with store.database.write_transaction() as database:
                database.executemany(
                    "INSERT INTO observation_events(run_id,session_id,project,mode,user_text,assistant_text,"
                    "stop_reason,committed,provider_id,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ((f"noise-{index}", "other-session", str(home / "other-project"), "chat", "Noise.",
                      "Recorded.", "done", 1, "fixture", "2026-11-17T00:00:00Z")
                     for index in range(MAX_OBSERVATIONS)),
                )
            assert store.append_completed(
                run_id="active-run", session_id="old-session", project=project,
                mode="chat", user_text=ACTIVE_QUERY, assistant_text="Recorded.", stop_reason="done",
            )
        committed = store.read_committed(project=project, session_id="old-session", scope=scope)
        picked = store.retrieve(ARCHIVED_QUERY, project=project, session_id="old-session", scope=scope)
        active = store.retrieve(ACTIVE_QUERY, project=project, session_id="old-session", scope=scope)
        block = render_retrieved_block(picked, 2000)
        _emit({
            "archived_committed": any(row["run_id"] == "archived-run" for row in committed),
            "archived_retrieved": any(row["run_id"] == "archived-run" for row in picked),
            "archived_rendered": ARCHIVED_QUERY in block,
            "active_retrieved": any(row["run_id"] == "active-run" for row in active),
        })
    finally:
        sys.meta_path.remove(finder)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--phase", choices=("schema7_visible", "schema7_trimmed", "capacity", "delete", "reset"),
                        required=True)
    parser.add_argument("--scope", choices=("user", "project", "session"), required=True)
    arguments = parser.parse_args()
    home: object = arguments.home
    phase: object = arguments.phase
    scope: object = arguments.scope
    if not isinstance(home, Path) or not isinstance(phase, str) or not isinstance(scope, str):
        raise TypeError("invalid reader arguments")
    run(home, phase, scope)


if __name__ == "__main__":
    main()
