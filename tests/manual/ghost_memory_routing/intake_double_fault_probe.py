"""Measure Ghost receipt/quarantine failure recovery through real task intake."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import Literal, Protocol, runtime_checkable

from codey.app.context import AppContext
from codey.app.task_services import build_task_deps
from codey.durable.model import TaskStatus
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.payload_types import mapping_fields
from codey.ghost.sqlite import GhostSQLite
from codey.operations.task_entry import run_task_submission
from codey.providers.base import ChatProvider
from codey.task.model import TaskSubmission
from tests.manual.ghost_memory_routing.inflight_delete_probe import DelayedProvider

Fault = Literal["healthy", "origin_only", "origin_and_quarantine"]
Action = Literal["delete_scope", "reset_all", "none"]
Scope = Literal["user", "project", "session"]
RepairAt = Literal["before_delete", "during_provider"]
REPAIR_TIMES: tuple[RepairAt, ...] = ("before_delete", "during_provider")
FAULT_MODES: tuple[Fault, ...] = ("healthy", "origin_only", "origin_and_quarantine")
RUN_ID = "fault-run"
SESSION_ID = "fault-session"
ORIGIN_TRIGGER = "probe_reject_input_origin"
QUARANTINE_TRIGGER = "probe_reject_input_quarantine"


@runtime_checkable
class InputQueuePort(Protocol):
    def create(self, identity: str, request: dict[str, object]) -> object: ...


@dataclass(frozen=True)
class Case:
    fault: Fault
    action: Action
    scope: Scope
    repair_at: RepairAt
    reopen: bool
    enabled: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "fault": self.fault, "action": self.action, "scope": self.scope,
            "repair_at": self.repair_at, "reopen": self.reopen, "accept_events": self.enabled,
        }


@dataclass(frozen=True)
class Result:
    case: Case
    admitted_before_entry: bool
    fault_was_observed: bool
    origin_at_intake: bool
    retired_at_intake: bool
    intake_warnings: int
    lifecycle_warnings: int
    canonical_survived_reopen: bool
    delete_ok: bool
    faults_restored: bool
    task_completed: bool
    sleep_completed: bool
    observation_rows: int
    continuity_rows: int

    @property
    def valid(self) -> bool:
        return all((
            self.admitted_before_entry, self.fault_was_observed, self.canonical_survived_reopen,
            self.delete_ok, self.faults_restored, self.task_completed, self.sleep_completed,
        ))

    @property
    def has_projection(self) -> bool:
        return bool(self.observation_rows or self.continuity_rows)

    def to_payload(self) -> dict[str, object]:
        return {
            **self.case.to_payload(), "valid": self.valid,
            "admitted_before_entry": self.admitted_before_entry,
            "fault_was_observed": self.fault_was_observed,
            "origin_at_intake": self.origin_at_intake, "retired_at_intake": self.retired_at_intake,
            "intake_warnings": self.intake_warnings, "lifecycle_warnings": self.lifecycle_warnings,
            "canonical_survived_reopen": self.canonical_survived_reopen, "delete_ok": self.delete_ok,
            "faults_restored": self.faults_restored, "task_completed": self.task_completed,
            "sleep_completed": self.sleep_completed,
            "observation_rows": self.observation_rows, "continuity_rows": self.continuity_rows,
        }


class RegistrationWarnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.intake = 0
        self.lifecycle = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "codey.app.ghost_input_admission" and record.getMessage().startswith(
            "Ghost intake registration failed:",
        ):
            self.intake += 1
        if record.name == "codey.operations.task_phases.lifecycle" and record.getMessage().startswith(
            "Ghost input registration failed:",
        ):
            self.lifecycle += 1


@contextmanager
def registration_warnings() -> Generator[RegistrationWarnings]:
    logger = logging.getLogger()
    handler = RegistrationWarnings()
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        handler.close()


def _fault_count(database: GhostSQLite) -> int:
    with database.connection() as connection:
        rows: list[sqlite3.Row] = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN (?,?)",
            (ORIGIN_TRIGGER, QUARANTINE_TRIGGER),
        ).fetchall()
    return len(rows)


def _install_faults(database: GhostSQLite, fault: Fault) -> int:
    with database.connection() as connection:
        if fault != "healthy":
            connection.execute(
                "CREATE TRIGGER probe_reject_input_origin BEFORE INSERT ON ghost_input_origins "
                "WHEN NEW.run_id='fault-run' "
                "BEGIN SELECT RAISE(ABORT,'fixture origin insert failure'); END",
            )
        if fault == "origin_and_quarantine":
            connection.execute(
                "CREATE TRIGGER probe_reject_input_quarantine BEFORE INSERT ON ghost_retired_inputs "
                "WHEN NEW.kind='observation' AND NEW.identity='fault-run' "
                "BEGIN SELECT RAISE(ABORT,'fixture quarantine insert failure'); END",
            )
    return _fault_count(database)


def _restore(database: GhostSQLite) -> bool:
    with database.connection() as connection:
        connection.execute("DROP TRIGGER IF EXISTS probe_reject_input_origin")
        connection.execute("DROP TRIGGER IF EXISTS probe_reject_input_quarantine")
    return _fault_count(database) == 0


def _input_metadata(database: GhostSQLite) -> tuple[bool, bool]:
    with database.connection() as connection:
        origin: sqlite3.Row | None = connection.execute(
            "SELECT run_id FROM ghost_input_origins WHERE run_id=?", (RUN_ID,),
        ).fetchone()
        retired: sqlite3.Row | None = connection.execute(
            "SELECT identity FROM ghost_retired_inputs WHERE kind='observation' AND identity=?", (RUN_ID,),
        ).fetchone()
    return origin is not None, retired is not None


def run_cell(home: Path, case: Case) -> Result:
    project = home / "project"
    project.mkdir(parents=True)
    database = GhostSQLite(home)
    trigger_count = _install_faults(database, case.fault)
    expected_trigger_count = {"healthy": 0, "origin_only": 1, "origin_and_quarantine": 2}[case.fault]
    ready, release = Event(), Event()
    provider = DelayedProvider(ready, release)

    def connect_provider(_provider_id: str) -> ChatProvider:
        return provider

    request = TaskSubmission(
        SESSION_ID, str(project), "What needs verification?", 2, False, "deepseek", intent="chat", run_id=RUN_ID,
    )
    with registration_warnings() as warnings, ExitStack() as resources:
        state = resources.enter_context(closing(AppContext(home, start_scheduler=False)))
        queue: object = state.input_queue
        if not isinstance(queue, InputQueuePort):
            raise TypeError("expected the production input facade")
        admitted = queue.create("fault-input", {
            "session_id": SESSION_ID, "project": request.project, "run_id": RUN_ID,
            "task": request.task, "provider": request.provider_id, "intent": request.intent,
            "max_turns": request.max_turns,
        })
        checkpoint = state.durable.task(RUN_ID)
        surface = GhostControlSurface.from_state_home(home)
        assert surface.inbox is not None and surface.observations is not None and surface.continuity is not None
        intake_warnings = warnings.intake
        origin, retired = _input_metadata(database)
        admitted_before_entry = (
            mapping_fields(admitted) and admitted.get("state") == "submitted"
            and checkpoint.status == TaskStatus.PENDING and checkpoint.phase == "accepted"
            and checkpoint.state.get("input_state") == "placed" and not ready.is_set()
            and not surface.observations.read_all() and not surface.continuity.list_items()
        )
        fault_was_observed = (
            trigger_count == expected_trigger_count and origin == (case.fault == "healthy")
            and retired == (case.fault == "origin_only")
            and (intake_warnings == 0 if case.fault == "healthy" else intake_warnings > 0)
        )
        if case.reopen:
            if not state.close():
                raise RuntimeError("received fixture did not close before reopen")
            state = resources.enter_context(closing(AppContext(home, start_scheduler=False)))
        canonical_survived_reopen = state.durable.task(RUN_ID) == checkpoint
        faults_restored = False
        if case.repair_at == "before_delete":
            faults_restored = _restore(database)
        if case.action == "none":
            delete_ok = True
        else:
            status, payload = surface.dispatch_action({
                "action": case.action, "scope": case.scope, "project": request.project,
                "session_id": SESSION_ID, "confirm": True,
            })
            delete_ok = status == 200 and payload.get("ok") is True
        assert surface.inbox.set_control("accept_events", case.enabled, scope="session", session_id=SESSION_ID)
        state.durable.claim_task(RUN_ID)
        claimed = state.durable.task(RUN_ID)
        assert claimed.phase == "dispatching"
        state.durable.start_task(RUN_ID, state=dict(claimed.state))
        deps = build_task_deps(state, connect_provider=connect_provider)

        def execute() -> None:
            run_task_submission(deps, request)

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(execute)
            try:
                if not ready.wait(20):
                    pending.result(timeout=1)
                    raise TimeoutError("task did not reach the deterministic transport")
                if case.repair_at == "during_provider":
                    faults_restored = _restore(database)
            finally:
                release.set()
            pending.result(timeout=20)
        sleep_completed = state.wait_for_ghost_sleep(timeout=10)
        terminal: object = state.run_registry.last_terminal_event()
        task_completed = ready.is_set() and mapping_fields(terminal) and terminal.get("stop_reason") == "done"
        return Result(
            case, admitted_before_entry, fault_was_observed, origin, retired, intake_warnings, warnings.lifecycle,
            canonical_survived_reopen, delete_ok, faults_restored, task_completed, sleep_completed,
            sum(row.get("run_id") == RUN_ID for row in surface.observations.read_all()),
            sum(item.source_ref == RUN_ID for item in surface.continuity.list_items()),
        )


def cases() -> tuple[Case, ...]:
    deletions: tuple[tuple[Action, Scope], ...] = (
        ("delete_scope", "user"), ("delete_scope", "project"),
        ("delete_scope", "session"), ("reset_all", "user"),
    )
    result: list[Case] = []
    for action, scope in deletions:
        for enabled in (True, False):
            for repair_at in REPAIR_TIMES:
                result.append(Case("origin_and_quarantine", action, scope, repair_at, False, enabled))
            result.append(Case("origin_and_quarantine", action, scope, "before_delete", True, enabled))
    for enabled in (True, False):
        for repair_at in REPAIR_TIMES:
            result.append(Case("origin_only", "delete_scope", "session", repair_at, False, enabled))
    for fault in FAULT_MODES:
        result.append(Case(fault, "none", "session", "before_delete", False, True))
    return tuple(result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    output: object = arguments.output
    if not isinstance(output, Path):
        raise TypeError("output must be a path")
    results: list[Result] = []
    payloads: list[dict[str, object]] = []
    matrix = cases()
    with TemporaryDirectory(prefix="codey-intake-double-fault-") as temporary:
        for index, case in enumerate(matrix):
            try:
                result = run_cell(Path(temporary) / str(index), case)
            except Exception as exc:
                payload = {**case.to_payload(), "valid": False, "error_type": type(exc).__name__}
            else:
                results.append(result)
                payload = result.to_payload()
            payloads.append(payload)
            print(json.dumps(payload, ensure_ascii=False), flush=True)
    healthy_calibration = any(
        result.valid and result.case.fault == "healthy" and result.case.action == "none"
        and result.observation_rows > 0 and result.continuity_rows > 0 for result in results
    )
    report: dict[str, object] = {
        "schema_version": 1, "baseline": "84c57aac", "expected_cells": len(matrix), "cells": payloads,
        "transport": "deterministic provider; actual InputQueue/task/Sleep; no model or HTTP; scheduler disabled",
        "invalid_cells": len(matrix) - sum(result.valid for result in results),
        "healthy_calibration_has_projection": healthy_calibration,
        "enabled_deleted_cells_with_old_input": sum(
            result.valid and result.case.enabled and result.case.action != "none" and result.has_projection
            for result in results
        ),
        "disabled_cells_with_old_input": sum(
            result.valid and not result.case.enabled and result.has_projection for result in results
        ),
        "single_fault_cells_with_projection": sum(
            result.valid and result.case.fault == "origin_only" and result.has_projection for result in results
        ),
        "double_fault_cells_with_projection": sum(
            result.valid and result.case.fault == "origin_and_quarantine" and result.has_projection for result in results
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report["invalid_cells"] or not healthy_calibration:
        raise SystemExit("incomplete fixture cells or missing healthy calibration; inspect report")


if __name__ == "__main__":
    main()
