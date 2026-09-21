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
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock


def _say(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def _run_write(state_home: str, steps: list) -> int:
    from tests.stress.world import StressWorld

    world = StressWorld(Path(state_home), seed=0)
    manifest: dict = {"effects": [], "batches": [], "ghosts": 0}
    checkpoint = ""
    for step in steps:
        action = step.get("do")
        if action == "accept":
            world.accept_operation(step["run"])
        elif action == "begin_provider":
            intent = world.provider_intent(step["run"])
            world.begin_provider(step["run"], intent)
            manifest["effects"].append(intent.effect_id)
        elif action == "settle_provider":
            world.settle_provider(step["run"], step["effect"])
        elif action == "tool_batch":
            batch = world.tool_batch_intent(step["run"], 1, tuple(step["refs"]))
            tool = world.tool_intent(step["run"], step["refs"][0])
            world.begin_batch(step["run"], batch, tool)
            manifest["batches"].append(batch.batch_id)
        elif action == "ghost":
            world.ghost_append(int(step.get("n", 1)), tag=step.get("tag", "kill"))
            manifest["ghosts"] += int(step.get("n", 1))
        elif action == "checkpoint":
            checkpoint = str(step["file"])
            _say({"manifest": manifest})
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


class _ShortWriteProxy:
    """File-object proxy that replays a torn OS write then dies.

    Either truncates one chunk mid-write (``short_chunk``) or stops the
    stream after ``stop_after_chunks`` full chunks. In both cases the bytes
    already delivered are flushed to the page cache -- exactly what a real
    kill would leave behind -- and ``_CrashPointReached`` aborts the path
    before flush/fsync/rename observe anything further. Everything else
    delegates to the real handle (including the context protocol, so the
    production ``with`` blocks and lock handling run unmodified).
    """

    def __init__(
        self,
        handle: Any,
        *,
        short_chunk: int | None = None,
        stop_after_chunks: int | None = None,
    ) -> None:
        self._handle = handle
        self._short_chunk = short_chunk
        self._stop_after_chunks = stop_after_chunks
        self._writes = 0

    def write(self, data: bytes) -> int:
        index = self._writes
        self._writes += 1
        if self._short_chunk is not None and index == self._short_chunk:
            self._handle.write(bytes(data)[: max(1, len(data) // 2)])
            self._handle.flush()
            raise _CrashPointReached(f"torn write at chunk {index}")
        if self._stop_after_chunks is not None and index >= self._stop_after_chunks:
            self._handle.flush()
            raise _CrashPointReached(f"stopped after {index} chunks")
        return self._handle.write(data)

    def __enter__(self) -> _ShortWriteProxy:
        self._handle.__enter__()
        return self

    def __exit__(self, *exc: object) -> bool:
        return bool(self._handle.__exit__(*exc))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)


def _faulty_fdopen(
    real_fdopen: Any,
    proxy_kwargs: dict,
    fd: int,
    *args: object,
    **kwargs: object,
) -> Any:
    return _ShortWriteProxy(real_fdopen(fd, *args, **kwargs), **proxy_kwargs)


def _crash_baseline(world: Any) -> str:
    """Commit one full op + provider intent every point asserts as intact."""
    world.accept_operation("run-cp-1")
    intent = world.provider_intent("run-cp-1")
    world.begin_provider("run-cp-1", intent)
    return intent.effect_id


def _point_session_torn_row(world: Any) -> dict:
    baseline = _crash_baseline(world)
    world.accept_operation("run-cp-2")
    doomed = world.provider_intent("run-cp-2")
    real_fdopen = os.fdopen
    with mock.patch.object(
        os,
        "fdopen",
        lambda fd, *a, **k: _faulty_fdopen(real_fdopen, {"short_chunk": 0}, fd, *a, **k),
    ):
        try:
            world.begin_provider("run-cp-2", doomed)
        except _CrashPointReached:
            pass
        else:
            raise AssertionError("torn-write fault did not fire")
    return {"point": "session-torn-row", "baseline_effect": baseline, "crashed_effect": doomed.effect_id}


def _point_session_full_no_ack(world: Any) -> dict:
    """Bytes fsynced, caller killed before observing the return.

    Unlike an idle-checkpoint kill, the fault fires *inside* the write
    path: the real fsync runs, then ``_CrashPointReached`` aborts before
    ``append_bytes_durable`` returns, so the caller never gets its ack.
    """
    baseline = _crash_baseline(world)
    world.accept_operation("run-cp-2")
    committed = world.provider_intent("run-cp-2")
    real_fsync = os.fsync

    def _die_after_fsync(fd: int) -> None:
        real_fsync(fd)
        raise _CrashPointReached("died after fsync, before ack")

    with mock.patch.object(os, "fsync", _die_after_fsync):
        try:
            world.begin_provider("run-cp-2", committed)
        except _CrashPointReached:
            pass
        else:
            raise AssertionError("post-fsync fault did not fire")
    return {"point": "session-full-no-ack", "baseline_effect": baseline, "crashed_effect": committed.effect_id}


def _point_ghost_partial_batch(world: Any) -> dict:
    baseline = _crash_baseline(world)
    real_fdopen = os.fdopen
    with mock.patch.object(
        os,
        "fdopen",
        lambda fd, *a, **k: _faulty_fdopen(real_fdopen, {"stop_after_chunks": 2}, fd, *a, **k),
    ):
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
        "durable_count": 2,
    }


def _point_ghost_torn_row(world: Any) -> dict:
    baseline = _crash_baseline(world)
    real_fdopen = os.fdopen
    with mock.patch.object(
        os,
        "fdopen",
        lambda fd, *a, **k: _faulty_fdopen(real_fdopen, {"short_chunk": 1}, fd, *a, **k),
    ):
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


def _point_primitive_replace(world: Any, *, before_rename: bool) -> dict:
    from codey.storage import atomic_io

    _crash_baseline(world)
    target = Path(world.state_home) / "cp-primitive" / "target.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b'{"v":1}')
    real_replace = os.replace

    def _blocked_replace(src: object, dst: object) -> None:
        raise _CrashPointReached("blocked before rename")

    def _die_after_replace(src: object, dst: object) -> None:
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
            _attempt_replace(atomic_io, target)
    else:
        with replace_patch:
            _attempt_replace(atomic_io, target)
    return {
        "point": "primitive-replace-pre-rename" if before_rename else "primitive-replace-post-rename",
        "target": str(target),
        "tmp_glob": ".target.json.*.tmp",
        "want_content": '{"v":1}' if before_rename else '{"v":2}',
    }


def _point_journal_torn_tail(world: Any) -> dict:
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


def _attempt_replace(atomic_io: Any, target: Path) -> None:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("write", "recover", "crashpoint"))
    parser.add_argument("--state-home", required=True)
    parser.add_argument("--steps", default="[]")
    parser.add_argument("--point", default="")
    parser.add_argument("--rendezvous", default="")
    args = parser.parse_args(argv)
    if args.mode == "write":
        return _run_write(args.state_home, json.loads(args.steps))
    if args.mode == "crashpoint":
        if not args.point or not args.rendezvous:
            raise ValueError("crashpoint mode needs --point and --rendezvous")
        return _run_crashpoint(args.state_home, args.point, args.rendezvous)
    return _run_recover(args.state_home)


if __name__ == "__main__":
    raise SystemExit(main())
