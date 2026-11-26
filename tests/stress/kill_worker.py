"""Child process for real kill -> restart -> recover tests.

NOT collected by pytest (no test_ prefix): the parent harness spawns it,
waits for a checkpoint file, kills it with ``Popen.kill()``
(TerminateProcess on Windows, SIGKILL on POSIX), then respawns it in
recover mode. Only fsync-backed surfaces are used here, so a kill at an
idle checkpoint loses nothing that ``write()`` already returned for.

Protocol (stdout, one JSON object per line, always flushed):
  write mode:  {"manifest": {...}} then {"checkpoint": path}, then idles.
  recover mode: {"canonical": {...}} then exits 0.
  crashpoint mode: like write mode, but the child first drives a REAL
    production write path through a test-only fault hook that stops at an
    internal critical point (short write, blocked rename, ...), leaving on
    disk exactly the byte prefix a kill at that point would leave. The
    parent then performs the REAL kill while the child idles: memory, fds
    and locks are gone, bytes on disk are identical. See
    tests/stress/test_crash_point_matrix.py for why the points are
    constructed instead of timed (a parent-side kill can never land
    between two syscalls deterministically).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from unittest import mock

from codey.durable.model import DurableMutation
from codey.ghost.projection import project_event
from codey.memory.contracts import MemoryEvent
from codey.storage import atomic_io
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    require_json_integer,
    require_json_string,
    require_json_strings,
)
from tests.stress.world import StressWorld


def _say(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def _run_write(state_home: str, steps: list[JsonObject]) -> int:
    from tests.stress.world import StressWorld

    world = StressWorld(Path(state_home), seed=0)
    effects: list[str] = []
    batches: list[str] = []
    ghosts = 0
    checkpoint = ""
    for step in steps:
        action = step.get("do")
        if action == "accept":
            world.accept_operation(require_json_string(step["run"], "run"))
        elif action == "begin_provider":
            intent = world.provider_intent(require_json_string(step["run"], "run"))
            world.begin_provider(require_json_string(step["run"], "run"), intent)
            effects.append(intent.effect_id)
        elif action == "settle_provider":
            world.settle_provider(require_json_string(step["run"], "run"), require_json_string(step["effect"], "effect"))
        elif action == "tool_batch":
            batch = world.tool_batch_intent(require_json_string(step["run"], "run"), 1, tuple(require_json_strings(step["refs"], "refs")))
            tool = world.tool_intent(require_json_string(step["run"], "run"), require_json_strings(step["refs"], "refs")[0])
            world.begin_batch(require_json_string(step["run"], "run"), batch, tool)
            batches.append(batch.batch_id)
        elif action == "ghost":
            world.ghost_append(require_json_integer(step.get("n", 1), "n"), tag=require_json_string(step.get("tag", "kill"), "tag"))
            ghosts += require_json_integer(step.get("n", 1), "n")
        elif action == "checkpoint":
            checkpoint = str(step["file"])
            _say({"manifest": {"effects": effects, "batches": batches, "ghosts": ghosts}})
            Path(checkpoint).write_text("ready\n", encoding="utf-8")
        else:
            raise ValueError(f"unknown step: {action!r}")
    if not checkpoint:
        raise ValueError("write mode needs a checkpoint step")
    while True:
        time.sleep(0.2)


def _run_recover(state_home: str) -> int:
    from tests.stress.world import StressWorld

    world = StressWorld(Path(state_home), seed=0)
    _say({"canonical": world.canonical()})
    return 0


class _CrashPointReached(Exception):
    """Test-only fault: stop a real write path exactly at a critical point."""


def _crash_baseline(world: StressWorld) -> str:
    """Commit one full op + provider intent every point asserts as intact."""
    world.accept_operation("run-cp-1")
    intent = world.provider_intent("run-cp-1")
    world.begin_provider("run-cp-1", intent)
    return intent.effect_id


def _point_session_torn_row(world: StressWorld) -> dict[str, object]:
    """A failed SQLite mutation is atomic: no partial runtime row is visible."""
    baseline = _crash_baseline(world)
    world.accept_operation("run-cp-2")
    doomed = world.provider_intent("run-cp-2")
    def _abort_before_commit(_mutation: object) -> None:
        raise _CrashPointReached("durable transaction aborted before commit")

    with mock.patch.object(world.durable.store, "commit", _abort_before_commit):
        try:
            world.begin_provider("run-cp-2", doomed)
        except _CrashPointReached:
            pass
        else:
            raise AssertionError("torn-write fault did not fire")
    return {"point": "session-torn-row", "baseline_effect": baseline, "crashed_effect": doomed.effect_id}


def _point_session_full_no_ack(world: StressWorld) -> dict[str, object]:
    """Bytes fsynced, caller killed before observing the return.

    Unlike an idle-checkpoint kill, the fault fires *inside* the write
    path: the real fsync runs, then ``_CrashPointReached`` aborts before
    ``append_bytes_durable`` returns, so the caller never gets its ack.
    """
    baseline = _crash_baseline(world)
    world.accept_operation("run-cp-2")
    committed = world.provider_intent("run-cp-2")
    real_commit = world.durable.store.commit

    def _die_after_commit(mutation: DurableMutation) -> None:
        real_commit(mutation)
        raise _CrashPointReached("died after durable commit, before ack")

    with mock.patch.object(world.durable.store, "commit", _die_after_commit):
        try:
            world.begin_provider("run-cp-2", committed)
        except _CrashPointReached:
            pass
        else:
            raise AssertionError("post-fsync fault did not fire")
    return {"point": "session-full-no-ack", "baseline_effect": baseline, "crashed_effect": committed.effect_id}


def _point_ghost_partial_batch(world: StressWorld) -> dict[str, object]:
    """Abort a Ghost SQLite transaction after all rows are staged.

    Ghost no longer appends JSONL chunks.  Its write contract is one SQLite
    transaction, so a crash in the commit path must leave an empty batch (the
    atomic counterpart of the old partial-file test).
    """
    baseline = _crash_baseline(world)
    def _abort_after_rows(connection: sqlite3.Connection, event: MemoryEvent) -> None:
        project_event(connection, event)
        raise _CrashPointReached("ghost transaction aborted before commit")

    with mock.patch("codey.ghost.backend.project_event", _abort_after_rows):
        try:
            world.ghost_append(5, tag="cp")
        except _CrashPointReached:
            pass
        else:
            raise AssertionError("stop-after fault did not fire")
    return {
        "point": "ghost-partial-batch",
        "baseline_effect": baseline,
        "ghost_ids": [f"ghost-cp-{index}" for index in range(1, 6)],
        "durable_count": 0,
    }


def _point_ghost_torn_row(world: StressWorld) -> dict[str, object]:
    """Abort a Ghost SQLite transaction before commit; no torn row is visible."""
    baseline = _crash_baseline(world)
    def _abort_after_rows(connection: sqlite3.Connection, event: MemoryEvent) -> None:
        project_event(connection, event)
        raise _CrashPointReached("ghost transaction aborted before commit")

    with mock.patch("codey.ghost.backend.project_event", _abort_after_rows):
        try:
            world.ghost_append(3, tag="cp")
        except _CrashPointReached:
            pass
        else:
            raise AssertionError("torn-write fault did not fire")
    return {
        "point": "ghost-torn-row",
        "baseline_effect": baseline,
        "ghost_ids": [f"ghost-cp-{index}" for index in range(1, 4)],
    }


def _point_primitive_replace(world: StressWorld, *, before_rename: bool) -> dict[str, object]:
    from codey.storage import atomic_io

    _crash_baseline(world)
    target = Path(world.state_home) / "cp-primitive" / "target.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'{"v":1}')
    real_replace = os.replace

    def _blocked_replace(src: str | Path, dst: str | Path) -> None:
        raise _CrashPointReached("blocked before rename")

    def _die_after_replace(src: str | Path, dst: str | Path) -> None:
        real_replace(src, dst)
        raise _CrashPointReached("died after rename")

    replace_patch = mock.patch.object(
        os, "replace", _blocked_replace if before_rename else _die_after_replace
    )
    if before_rename:
        # A real SIGKILL between tmp-fsync and rename never runs the
        # production ``finally`` cleanup: keep the orphan tmp on disk so
        # recovery must prove it ignores it.
        with replace_patch, mock.patch.object(atomic_io, "_cleanup_temp_file", lambda path: None):
            _attempt_replace(target)
    else:
        with replace_patch:
            _attempt_replace(target)
    return {
        "point": "primitive-replace-pre-rename" if before_rename else "primitive-replace-post-rename",
        "target": str(target),
        "tmp_glob": ".*.tmp",
        "want_content": '{"v":1}' if before_rename else '{"v":2}',
    }


def _point_journal_torn_tail(world: StressWorld) -> dict[str, object]:
    baseline = _crash_baseline(world)
    world.journal.append("cp-baseline", run="run-cp-1")
    journal_path = world.journal.path
    assert journal_path is not None
    torn = (json.dumps({"event": "cp-torn", "detail": "x" * 200}, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(journal_path, os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, torn[: len(torn) // 2])
    finally:
        os.close(fd)
    return {"point": "journal-torn-tail", "baseline_effect": baseline, "journal": str(journal_path)}


def _attempt_replace(target: Path) -> None:
    try:
        atomic_io.write_bytes_atomic(target, b'{"v":2}')
    except _CrashPointReached:
        pass
    else:
        raise AssertionError("rename fault did not fire")


_CRASHPOINTS = (
    "session-torn-row",
    "session-full-no-ack",
    "ghost-partial-batch",
    "ghost-torn-row",
    "primitive-replace-pre-rename",
    "primitive-replace-post-rename",
    "journal-torn-tail",
)


def _run_crashpoint(state_home: str, point: str, rendezvous: str) -> int:
    from tests.stress.world import StressWorld

    if point not in _CRASHPOINTS:
        raise ValueError(f"unknown crash point: {point!r}")
    world = StressWorld(Path(state_home), seed=0)
    if point == "session-torn-row":
        manifest = _point_session_torn_row(world)
    elif point == "session-full-no-ack":
        manifest = _point_session_full_no_ack(world)
    elif point == "ghost-partial-batch":
        manifest = _point_ghost_partial_batch(world)
    elif point == "ghost-torn-row":
        manifest = _point_ghost_torn_row(world)
    elif point == "primitive-replace-pre-rename":
        manifest = _point_primitive_replace(world, before_rename=True)
    elif point == "primitive-replace-post-rename":
        manifest = _point_primitive_replace(world, before_rename=False)
    else:
        manifest = _point_journal_torn_tail(world)
    _say({"manifest": manifest})
    Path(rendezvous).write_text("ready\n", encoding="utf-8")
    while True:
        time.sleep(0.2)


class _Arguments(argparse.Namespace):
    mode: str
    state_home: str
    steps: str
    point: str
    rendezvous: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("write", "recover", "crashpoint"))
    parser.add_argument("--state-home", required=True)
    parser.add_argument("--steps", default="[]")
    parser.add_argument("--point", default="")
    parser.add_argument("--rendezvous", default="")
    args = parser.parse_args(argv, namespace=_Arguments())
    if args.mode == "write":
        raw_steps: object = json.loads(args.steps)
        if not isinstance(raw_steps, list):
            raise ValueError("steps must be a list")
        return _run_write(args.state_home, [decode_json_object(step) for step in raw_steps])
    if args.mode == "crashpoint":
        if not args.point or not args.rendezvous:
            raise ValueError("crashpoint mode needs --point and --rendezvous")
        return _run_crashpoint(args.state_home, args.point, args.rendezvous)
    return _run_recover(args.state_home)


if __name__ == "__main__":
    raise SystemExit(main())
