"""A warm historical retrieval consumer must respect archived observation windows."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.observations import MAX_OBSERVATIONS
from codey.ghost.payload_types import decoded_object
from tests.manual.ghost_memory_routing.legacy_observation_reader import ACTIVE_QUERY, ARCHIVED_QUERY

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _reader(home: Path, phase: str, scope: str) -> Generator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.manual.ghost_memory_routing.legacy_observation_reader",
         "--home", str(home), "--phase", phase, "--scope", scope],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    try:
        assert process.stdout is not None
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(process.stdout.readline)
            try:
                ready = decoded_object(pending.result(timeout=25))
            except BaseException:
                process.kill()
                raise
        assert ready["ready"] is True and ready["schema"] == 7
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.parametrize("scope", ["user", "project", "session"])
@pytest.mark.parametrize("phase", ["schema7_visible", "schema7_trimmed", "capacity", "delete", "reset"])
def test_warm_legacy_retrieval_respects_observation_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str, phase: str,
) -> None:
    for name in ("GHOST_EMBEDDING_URL", "GHOST_EMBEDDING_MODEL", "GHOST_EMBEDDING_DIMENSIONS"):
        monkeypatch.delenv(name, raising=False)
    with _reader(tmp_path, phase, scope) as process:
        if phase in {"capacity", "delete", "reset"}:
            surface = GhostControlSurface.from_state_home(tmp_path)
            store = surface.observations
            assert store is not None and store.vector_memory is None
            project = str(tmp_path / "project")
            with store.database.write_transaction() as database:
                database.executemany(
                    "INSERT INTO observation_events(run_id,session_id,project,mode,user_text,assistant_text,"
                    "stop_reason,committed,provider_id,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    ((f"noise-{index}", "other-session", str(tmp_path / "other-project"), "chat", "Noise.",
                      "Recorded.", "done", 1, "fixture", "2026-11-17T00:00:00Z")
                     for index in range(MAX_OBSERVATIONS)),
                )
            assert store.append_completed(
                run_id="capacity-run", session_id="other-session", project=str(tmp_path / "other-project"),
                mode="chat", user_text="Noise.", assistant_text="Recorded.", stop_reason="done",
            )
            if phase in {"delete", "reset"}:
                action: dict[str, object] = {"action": "reset_all", "confirm": True}
                if phase == "delete":
                    action.update(action="delete_scope", scope=scope, project=project, session_id="old-session")
                status, payload = surface.dispatch_action(action)
                assert status == 200, payload
            assert store.append_completed(
                run_id="active-run", session_id="old-session", project=project,
                mode="chat", user_text=ACTIVE_QUERY, assistant_text="Recorded.", stop_reason="done",
            )
            assert not store.retrieve(ARCHIVED_QUERY, project=project, session_id="old-session", scope=scope)
            assert store.retrieve(ACTIVE_QUERY, project=project, session_id="old-session", scope=scope)
            assert len(store.read_all()) <= MAX_OBSERVATIONS
            with store.database.connection() as database:
                retained: sqlite3.Row | None = database.execute(
                    "SELECT 1 FROM observation_events WHERE run_id='archived-run'",
                ).fetchone()
                assert retained is not None, "invalid fixture: archived history not retained"
        output, errors = process.communicate("go\n", timeout=30)
        assert process.wait(timeout=10) == 0, errors
        result = decoded_object(output.strip())
        for name in ("archived_committed", "archived_retrieved", "archived_rendered", "active_retrieved"):
            assert type(result[name]) is bool, result
        assert result["active_retrieved"] == (phase != "schema7_visible"), result
        expected = phase == "schema7_visible"
        observed = tuple(result[name] for name in ("archived_committed", "archived_retrieved", "archived_rendered"))
        assert observed == (expected, expected, expected), f"legacy consumer ignored the archive window: {result}"
