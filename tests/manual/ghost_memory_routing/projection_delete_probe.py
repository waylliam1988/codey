"""Reproduce source-read/delete/write races using the real Ghost stores."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.graph_store import GhostGraph
from codey.ghost.record_store import GhostRecords
from tests.manual.ghost_memory_routing.deletion_runtime_probe import _seed


@dataclass(frozen=True)
class ProjectionCell:
    action: str
    writer: str
    order: str
    repetition: int
    delete_ok: bool
    writer_ok: bool
    resurrected: bool
    rebuild_ok: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "action": self.action, "writer": self.writer, "order": self.order,
            "repetition": self.repetition, "delete_ok": self.delete_ok,
            "writer_ok": self.writer_ok, "resurrected": self.resurrected, "rebuild_ok": self.rebuild_ok,
        }


def seed_derived(surface: GhostControlSurface, project: str) -> None:
    _seed(surface, project)
    assert surface.continuity is not None
    assert surface.work_queue is not None
    assert surface.affinity is not None
    assert surface.sleep is not None
    assert surface.continuity.sync_from_sources(
        hebbian_store=surface.hebbian, user_focus_excerpt="What remains to verify?",
        run_id="old-run", session_id="", project=project, mode="chat",
    ).ok
    assert surface.work_queue.sync_from_sources(
        continuity_store=surface.continuity, session_id="session", project=project,
    ).ok
    assert surface.affinity.sync_from_sources(
        hebbian_store=surface.hebbian, work_queue_store=surface.work_queue,
        session_id="session", project=project,
    ).ok
    surface.sleep.run_once(run_id="old-run", session_id="session", project=project)
    for writer in ("continuity", "work_queue", "affinity", "sleep"):
        assert has_projection(surface, writer, project=project), f"missing {writer} seed"


def _operation(surface: GhostControlSurface, writer: str, project: str) -> Callable[[], bool]:
    assert surface.continuity is not None
    assert surface.work_queue is not None
    assert surface.affinity is not None
    assert surface.sleep is not None
    if writer == "continuity":
        return lambda: surface.continuity is not None and surface.continuity.sync_from_sources(
            hebbian_store=surface.hebbian, session_id="session", project=project,
        ).ok
    if writer == "work_queue":
        return lambda: surface.work_queue is not None and surface.work_queue.sync_from_sources(
            continuity_store=surface.continuity, session_id="session", project=project,
        ).ok
    if writer == "affinity":
        return lambda: surface.affinity is not None and surface.affinity.sync_from_sources(
            hebbian_store=surface.hebbian, work_queue_store=surface.work_queue,
            session_id="session", project=project,
        ).ok

    def sleep() -> bool:
        assert surface.sleep is not None
        report = surface.sleep.run_once(run_id="old-run", session_id="session", project=project)
        return report.steps[-1].ok

    return sleep


def has_projection(surface: GhostControlSurface, writer: str, *, project: str = "") -> bool:
    if writer == "continuity":
        assert surface.continuity is not None
        return bool(surface.continuity.list_items())
    if writer == "work_queue":
        assert surface.work_queue is not None
        return bool(surface.work_queue.list_items())
    if writer == "affinity":
        assert surface.affinity is not None
        return bool(surface.affinity.list_nodes(project=project, session_id="session"))
    assert surface.sleep is not None
    return bool(surface.sleep.export_state()["sleep"])


def _target_records(surface: GhostControlSurface, writer: str) -> GhostRecords | GhostGraph:
    if writer == "continuity":
        assert surface.continuity is not None
        return surface.continuity.records
    if writer == "work_queue":
        assert surface.work_queue is not None
        return surface.work_queue.records
    if writer == "affinity":
        assert surface.affinity is not None
        return surface.affinity.graph
    assert surface.sleep is not None
    return surface.sleep.records


def cell(home: Path, action: str, writer: str, order: str, repetition: int) -> ProjectionCell:
    surface = GhostControlSurface.from_state_home(home)
    project = str(home / "project")
    seed_derived(surface, project)
    owner = GhostControlSurface.from_state_home(home)
    records = _target_records(owner, writer)
    original = records.transaction
    ready, release = Event(), Event()

    @contextmanager
    def before_write(*, write: bool = True, deleting: bool = False) -> Generator[None]:
        if write and not ready.is_set():
            ready.set()
            if not release.wait(15):
                raise TimeoutError("delete did not release projection writer")
        transaction = original(write=write, deleting=True) if deleting else original(write=write)
        with transaction:
            yield

    with patch.object(records, "transaction", before_write), ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(_operation(owner, writer, project))
        try:
            if not ready.wait(15):
                raise TimeoutError(f"{writer} did not enter write boundary")
            if order == "writer_first":
                release.set()
                writer_ok = pending.result(timeout=15)
            status, result = surface.dispatch_action({
                "action": action, "scope": "project", "project": project, "confirm": True,
            })
            release.set()
            if order != "writer_first":
                writer_ok = pending.result(timeout=15)
        finally:
            release.set()
    reopened = GhostControlSurface.from_state_home(home)
    assert reopened.continuity is not None
    assert reopened.work_queue is not None
    assert reopened.affinity is not None
    rebuilt = (
        reopened.continuity.rebuild_from_events()
        and reopened.work_queue.rebuild_from_events() and reopened.affinity.rebuild_from_events()
    )
    return ProjectionCell(
        action, writer, order, repetition, status == 200 and result.get("ok") is True,
        writer_ok, has_projection(reopened, writer, project=project), rebuilt,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    output: object = arguments.output
    if not isinstance(output, Path):
        raise TypeError("output must be a path")
    cells: list[ProjectionCell] = []
    with TemporaryDirectory(prefix="codey-projection-delete-") as temporary:
        for action in ("delete_scope", "reset_all"):
            for writer in ("continuity", "work_queue", "affinity", "sleep"):
                for order in ("writer_first", "delete_first"):
                    for repetition in range(2):
                        cells.append(cell(Path(temporary) / str(len(cells)), action, writer, order, repetition))
    payload: dict[str, object] = {
        "schema_version": 1, "model": None, "cell_count": len(cells),
        "resurrections": sum(item.resurrected for item in cells),
        "all_deletes_ok": all(item.delete_ok for item in cells),
        "cells": [item.to_payload() for item in cells],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "cells"}))


if __name__ == "__main__":
    main()
