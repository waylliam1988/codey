"""Measure deletion during a real task before its first Ghost settlement."""

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
from codey.app.task_services import build_task_deps
from codey.ghost.control_surface import GhostControlSurface
from codey.operations.task_entry import run_task_submission
from codey.providers.base import ChatProvider
from codey.task.model import TaskSubmission


class DelayedProvider:
    name = "Deterministic task transport"
    location = "fixture://inflight-delete"

    def __init__(self, ready: Event, release: Event) -> None:
        self.ready = ready
        self.release = release

    def new_chat(self, timeout: float | None = None) -> None:
        pass

    def send(self, text: str, timeout: float | None = None) -> str:
        self.ready.set()
        if not self.release.wait(20):
            raise TimeoutError("delete did not release task transport")
        return "A deterministic completed reply."

    def close(self) -> None:
        pass


@dataclass(frozen=True)
class InflightCell:
    action: str
    accept_events: bool
    repetition: int
    empty_before_delete: bool
    delete_ok: bool
    task_completed: bool
    observation_rows: int
    continuity_rows: int

    def to_payload(self) -> dict[str, object]:
        return {
            "action": self.action, "accept_events": self.accept_events, "repetition": self.repetition,
            "empty_before_delete": self.empty_before_delete, "delete_ok": self.delete_ok,
            "task_completed": self.task_completed, "observation_rows": self.observation_rows,
            "continuity_rows": self.continuity_rows,
        }


def cell(home: Path, action: str, accept_events: bool, repetition: int) -> InflightCell:
    ready, release = Event(), Event()
    provider = DelayedProvider(ready, release)

    def connect_provider(_provider_id: str) -> ChatProvider:
        return provider

    with closing(AppContext(home)) as state:
        surface = GhostControlSurface.from_state_home(home)
        assert surface.inbox is not None
        assert surface.observations is not None
        assert surface.continuity is not None
        deps = build_task_deps(state, connect_provider=connect_provider)
        request = TaskSubmission(
            session_id="inflight-session", project=None, task="What should we verify next?",
            max_turns=2, continue_task=False, provider_id="deepseek", intent="chat", run_id="inflight-run",
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(run_task_submission, deps, request)
            try:
                if not ready.wait(20):
                    # Surface setup failures rather than counting them as a
                    # successful deletion or a negative task result.
                    pending.result(timeout=1)
                    raise TimeoutError("task did not reach its transport")
                empty = not surface.observations.read_all() and not surface.continuity.list_items()
                status, payload = surface.dispatch_action({
                    "action": action, "scope": "session", "session_id": request.session_id, "confirm": True,
                })
                assert surface.inbox.set_control(
                    "accept_events", accept_events, scope="session", session_id=request.session_id,
                )
                release.set()
                pending.result(timeout=20)
            finally:
                release.set()
        assert state.wait_for_ghost_sleep(timeout=10)
        terminal: Mapping[str, object] = state.run_registry.last_terminal_event() or {}
        observations = surface.observations.read_all()
        continuity = surface.continuity.list_items()
        return InflightCell(
            action, accept_events, repetition, empty, status == 200 and payload.get("ok") is True,
            terminal.get("stop_reason") == "done",
            sum(row.get("run_id") == request.run_id for row in observations),
            sum(item.source_ref == request.run_id for item in continuity),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output: object = args.output
    if not isinstance(output, Path):
        raise TypeError("output must be a path")
    cells: list[InflightCell] = []
    with TemporaryDirectory(prefix="codey-inflight-delete-") as temporary:
        for action in ("delete_scope", "reset_all"):
            for enabled in (True, False):
                for repetition in range(2):
                    home = Path(temporary) / f"{action}-{enabled}-{repetition}"
                    result = cell(home, action, enabled, repetition)
                    cells.append(result)
                    print(json.dumps(result.to_payload(), ensure_ascii=False), flush=True)
    report: dict[str, object] = {
        "schema_version": 1,
        "transport": "deterministic fixture; no model or HTTP",
        "cells": [item.to_payload() for item in cells],
        "invalid_cells": sum(not (item.empty_before_delete and item.delete_ok and item.task_completed) for item in cells),
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
