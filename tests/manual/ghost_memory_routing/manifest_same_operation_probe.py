"""Freeze same-operation delete resumes around real runtime learning."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import Literal

from codey.app.context import AppContext
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.deletion_manifest import DeletionManifest, new_manifest
from codey.ghost.deletion_manifest_coordinator import (
    GHOST_STORE_NAMES,
    DeletionManifestCoordinator,
    DeletionTarget,
)
from codey.ghost.deletion_manifest_store import DeletionManifestStore
from tests.manual.ghost_memory_routing.deletion_runtime_probe import _targets
from tests.test_ghost_received_input_lifetime import _receive, _run

Mode = Literal["sequential", "before_ack", "after_completion"]
MODES: tuple[Mode, ...] = ("sequential", "before_ack", "after_completion")
SCOPES: tuple[tuple[str, str], ...] = (
    ("delete_scope", "user"), ("delete_scope", "project"),
    ("delete_scope", "session"), ("reset_all", "all"),
)


@dataclass
class Schedule:
    mode: Mode
    first_entered: Event = field(default_factory=Event)
    second_entered: Event = field(default_factory=Event)
    first_deleted: Event = field(default_factory=Event)
    second_deleted: Event = field(default_factory=Event)
    release_second: Event = field(default_factory=Event)


@dataclass
class Calls:
    results: list[object] = field(default_factory=list)


@dataclass(frozen=True)
class Cell:
    action: str
    scope: str
    mode: Mode
    repetition: int
    old_rows: int
    first_status: str
    reader_status: str
    second_status: str
    second_error: str
    events: int
    first_calls: tuple[object, ...]
    second_calls: tuple[object, ...]
    fresh_before: int
    fresh_after: int
    overlap_reached: bool

    @property
    def valid(self) -> bool:
        return (
            self.old_rows == 1 and self.first_status == "completed"
            and self.reader_status == "completed" and self.fresh_before == 1
            and self.overlap_reached
        )

    @property
    def passed(self) -> bool:
        return (
            self.valid and len(self.first_calls) + len(self.second_calls) == 1
            and self.second_status == "completed" and not self.second_error
            and self.fresh_after == 1
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "action": self.action, "scope": self.scope, "mode": self.mode,
            "repetition": self.repetition, "valid": self.valid, "passed": self.passed,
            "old_rows": self.old_rows, "first_status": self.first_status,
            "reader_status": self.reader_status, "second_status": self.second_status,
            "second_error": self.second_error, "events": self.events,
            "first_calls": self.first_calls, "second_calls": self.second_calls,
            "fresh_before": self.fresh_before, "fresh_after": self.fresh_after,
            "overlap_reached": self.overlap_reached,
        }


def _wait(event: Event) -> None:
    if not event.wait(20):
        raise TimeoutError("same-operation experiment did not reach its frozen boundary")


def _coordinator(
    home: Path, schedule: Schedule, calls: Calls, *, second: bool,
) -> DeletionManifestCoordinator:
    surface = GhostControlSurface.from_state_home(home)
    assert surface.observations is not None
    targets = _targets(surface)
    original = targets["observations"]

    def mutate(scope: str = "", *, project: str = "", session_id: str = "") -> object:
        if second:
            schedule.second_entered.set()
            if schedule.mode == "before_ack":
                _wait(schedule.first_deleted)
            else:
                _wait(schedule.release_second)
        elif schedule.mode != "sequential":
            schedule.first_entered.set()
            _wait(schedule.second_entered)
        result = (
            original.delete_scope(scope, project=project, session_id=session_id)
            if scope else original.reset_all()
        )
        calls.results.append(result)
        if second:
            schedule.second_deleted.set()
            if schedule.mode == "before_ack":
                _wait(schedule.release_second)
        else:
            schedule.first_deleted.set()
            if schedule.mode == "before_ack":
                _wait(schedule.second_deleted)
        return result

    def reset() -> object:
        return mutate()

    targets["observations"] = DeletionTarget(mutate, reset)
    return DeletionManifestCoordinator(DeletionManifestStore(surface.observations.database), targets)


def _rows(home: Path, run_id: str) -> int:
    surface = GhostControlSurface.from_state_home(home)
    assert surface.observations is not None
    return sum(item.get("run_id") == run_id for item in surface.observations.read_all())


def run_cell(home: Path, action: str, scope: str, mode: Mode, repetition: int) -> Cell:
    schedule = Schedule(mode)
    first_calls, second_calls = Calls(), Calls()
    with closing(AppContext(home, start_scheduler=False)) as state:
        project = str(home / "project")
        _run(state, _receive(state, project, run_id="old-run"))
        old_rows = _rows(home, "old-run")
        manifest = new_manifest(
            operation_id="same-operation", action=action, scope=scope,
            project=project, session_id="received-session",
            requested_at="2026-11-17T00:00:00Z", retention_class="tombstone",
            stores=GHOST_STORE_NAMES,
        )
        first = _coordinator(home, schedule, first_calls, second=False)
        second = _coordinator(home, schedule, second_calls, second=True)
        pending_second: Future[DeletionManifest] | None = None
        with ThreadPoolExecutor(max_workers=2) as executor:
            try:
                pending_first = executor.submit(first.run, manifest)
                if mode != "sequential":
                    _wait(schedule.first_entered)
                    pending_second = executor.submit(second.run, manifest)
                completed = pending_first.result(timeout=25)
                reader_status = second.manifests.get(manifest.operation_id).status
                # This input is strictly later than completed, not merely
                # concurrent with a delete which has yet to finish.
                _run(state, _receive(state, project, run_id="fresh-run"))
                fresh_before = _rows(home, "fresh-run")
                schedule.release_second.set()
                if pending_second is None:
                    pending_second = executor.submit(second.run, manifest)
                second_status, second_error = "", ""
                try:
                    second_status = pending_second.result(timeout=25).status
                except ValueError as exc:
                    second_error = f"{type(exc).__name__}: {exc}"
            finally:
                schedule.release_second.set()
                schedule.second_entered.set()
                schedule.first_deleted.set()
                schedule.second_deleted.set()
        overlap = mode == "sequential" or bool(second_calls.results)
        return Cell(
            action, scope, mode, repetition, old_rows, completed.status, reader_status,
            second_status, second_error, len(second.manifests.events(manifest.operation_id)),
            tuple(first_calls.results), tuple(second_calls.results),
            fresh_before, _rows(home, "fresh-run"), overlap,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    output: object = arguments.output
    if not isinstance(output, Path):
        raise TypeError("output must be a path")
    cells: list[Cell] = []
    with TemporaryDirectory(prefix="codey-manifest-same-operation-") as temporary:
        for action, scope in SCOPES:
            for mode in MODES:
                for repetition in range(2):
                    cells.append(run_cell(Path(temporary) / str(len(cells)), action, scope, mode, repetition))
    payload: dict[str, object] = {
        "schema_version": 1, "baseline": "47410ec6", "model": None,
        "cells": [cell.to_payload() for cell in cells], "cell_count": len(cells),
        "valid": sum(cell.valid for cell in cells), "passed": sum(cell.passed for cell in cells),
        "fresh_losses": sum(cell.valid and cell.fresh_after == 0 for cell in cells),
        "second_errors": sum(bool(cell.second_error) for cell in cells),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary: dict[str, object] = {key: value for key, value in payload.items() if key != "cells"}
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
