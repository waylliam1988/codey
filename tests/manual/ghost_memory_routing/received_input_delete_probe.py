"""Measure deletion after ordinary input admission but before execution entry."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from codey.app.context import AppContext
from codey.app.input_queue import DurableConversationInputQueue
from codey.app.task_services import build_task_deps
from codey.durable.model import TaskStatus
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.payload_types import mapping_fields
from codey.operations.task_entry import run_task_submission
from codey.providers.base import ChatProvider
from codey.task.model import TaskSubmission
from tests.manual.ghost_memory_routing.inflight_delete_probe import DelayedProvider


@dataclass(frozen=True)
class ReceivedCell:
    action: str
    scope: str
    accept_events: bool
    admitted_before_entry: bool
    delete_ok: bool
    task_completed: bool
    observation_rows: int
    continuity_rows: int

    def to_payload(self) -> dict[str, object]:
        return {
            "action": self.action, "scope": self.scope, "accept_events": self.accept_events,
            "admitted_before_entry": self.admitted_before_entry, "delete_ok": self.delete_ok,
            "task_completed": self.task_completed, "observation_rows": self.observation_rows,
            "continuity_rows": self.continuity_rows,
        }


def received_cell(home: Path, action: str, scope: str, enabled: bool) -> ReceivedCell:
    ready, release = Event(), Event()
    release.set()
    provider = DelayedProvider(ready, release)

    def connect_provider(_provider_id: str) -> ChatProvider:
        return provider

    project = home / "project"
    project.mkdir(parents=True)
    request = TaskSubmission(
        "received-session", str(project), "What needs verification?", 2, False, "deepseek",
        intent="chat", run_id="received-run",
    )
    with closing(AppContext(home, start_scheduler=False)) as state:
        queue: object = state.input_queue
        if not isinstance(queue, DurableConversationInputQueue):
            raise TypeError("expected the production durable input queue")
        admitted: object = queue.create("received-input", {
            "session_id": request.session_id, "project": request.project, "run_id": request.run_id,
            "task": request.task, "provider": request.provider_id, "intent": request.intent,
            "max_turns": request.max_turns,
        })
        surface = GhostControlSurface.from_state_home(home)
        assert surface.inbox is not None and surface.observations is not None and surface.continuity is not None
        checkpoint = state.durable.task(request.run_id)
        before = (
            mapping_fields(admitted) and admitted.get("state") == "submitted"
            and checkpoint.status == TaskStatus.PENDING and checkpoint.phase == "accepted"
            and checkpoint.state.get("input_state") == "placed" and not ready.is_set()
            and not surface.observations.read_all() and not surface.continuity.list_items()
        )
        status, payload = surface.dispatch_action({
            "action": action, "scope": scope, "session_id": request.session_id,
            "project": request.project, "confirm": True,
        })
        assert surface.inbox.set_control("accept_events", enabled, scope="session", session_id=request.session_id)
        # Reproduce the scheduler/worker ownership transitions. Entering the
        # raw operation on an accepted checkpoint would test a broken fixture,
        # rather than a normally dispatched task.
        state.durable.claim_task(request.run_id)
        claimed = state.durable.task(request.run_id)
        assert claimed.phase == "dispatching"
        state.durable.start_task(request.run_id, state=dict(claimed.state))
        deps = build_task_deps(state, connect_provider=connect_provider)
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(run_task_submission, deps, request).result(timeout=20)
        assert state.wait_for_ghost_sleep(timeout=10)
        terminal: Mapping[str, object] = state.run_registry.last_terminal_event() or {}
        return ReceivedCell(
            action, scope, enabled, before, status == 200 and payload.get("ok") is True,
            ready.is_set() and terminal.get("stop_reason") == "done",
            sum(row.get("run_id") == request.run_id for row in surface.observations.read_all()),
            sum(item.source_ref == request.run_id for item in surface.continuity.list_items()),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output: object = args.output
    if not isinstance(output, Path):
        raise TypeError("output must be a path")
    cells: list[ReceivedCell] = []
    with TemporaryDirectory(prefix="codey-received-delete-") as temporary:
        for action, scope in (("delete_scope", "user"), ("delete_scope", "project"),
                              ("delete_scope", "session"), ("reset_all", "user")):
            for enabled in (True, False):
                result = received_cell(Path(temporary) / f"{action}-{scope}-{enabled}", action, scope, enabled)
                cells.append(result)
                print(json.dumps(result.to_payload(), ensure_ascii=False), flush=True)
    report: dict[str, object] = {
        "schema_version": 1, "baseline": "78c907de",
        "transport": "deterministic fixture; real input queue and task runtime; scheduler disabled; no model or HTTP",
        "admission": "placed, pending dispatch; explicit canonical claim/start; not busy-queue placement",
        "cells": [item.to_payload() for item in cells],
        "invalid_cells": sum(not (item.admitted_before_entry and item.delete_ok and item.task_completed) for item in cells),
        "enabled_cells_with_old_input": sum(
            item.accept_events and bool(item.observation_rows or item.continuity_rows) for item in cells
        ),
        "disabled_cells_with_old_input": sum(
            not item.accept_events and bool(item.observation_rows or item.continuity_rows) for item in cells
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report["invalid_cells"]:
        raise SystemExit("incomplete fixture cells; inspect report")


if __name__ == "__main__":
    main()
