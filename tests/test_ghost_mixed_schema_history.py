"""Already-running historical owners cannot rewrite an upgraded event history."""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.graph_store import GhostGraph
from codey.ghost.payload_types import decoded_object
from tests.test_ghost_capacity_history_retention import _owner
from tests.test_ghost_scope_history_retention import STORES, _history, _visible

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("operation", ["compact", "reset"])
@pytest.mark.parametrize("phase", ["schema7", "upgraded", "reset_after_upgrade"])
def test_warm_legacy_mutation_preserves_upgraded_history(
    tmp_path: Path, store: str, operation: str, phase: str,
) -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.manual.ghost_memory_routing.mixed_schema_worker",
         "--home", str(tmp_path), "--store", store, "--operation", operation],
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
        before: tuple[tuple[object, ...], ...] = ()
        surface: GhostControlSurface | None = None
        if phase != "schema7":
            surface = GhostControlSurface.from_state_home(tmp_path)
            if phase == "reset_after_upgrade":
                status, payload = surface.dispatch_action({"action": "reset_all", "confirm": True})
                assert status == 200, payload
            elif store == "observations":
                assert surface.observations is not None
                assert surface.observations.append_completed(
                    run_id="current-fresh-run", session_id="session", mode="chat", user_text="Current fact",
                    assistant_text="Recorded.", stop_reason="done",
                )
            else:
                owner = _owner(surface, store)
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
            before = _history(surface, store)
            assert before and _visible(surface, store) == (phase == "upgraded")
        output, errors = process.communicate("go\n", timeout=30)
        assert process.wait(timeout=10) == 0, errors
        result = decoded_object(output.strip())
        outcome = result.get("outcome")
        assert isinstance(outcome, str) and outcome in {"completed", "rejected"}, result
        if phase == "schema7":
            assert result["outcome"] == "completed", result
        else:
            assert surface is not None
            assert _history(surface, store)[:len(before)] == before, "legacy warm owner rewrote upgraded history"
            assert _visible(surface, store) == (phase == "upgraded")
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
