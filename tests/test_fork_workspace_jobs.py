"""Exercise job ownership with simulated Git; never create a worktree."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from codey.app import conversation_routes, fork_workspaces
from codey.app.context import AppContext
from codey.storage.local_store import write_json_atomic

CHILD = "fork_" + "a" * 32


def test_start_cannot_publish_a_worker_after_context_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = AppContext(tmp_path / "state", start_scheduler=False)
    jobs = fork_workspaces.ForkWorkspaces(ctx, tmp_path)
    ctx.fork_workspaces = jobs
    checked, release, started = Event(), Event(), Event()
    shutdown_requested = ctx.shutdown_requested

    def delayed_shutdown_check() -> bool:
        # Capture the real pre-close result, then stop the admission thread
        # before it enters the job lock. Shutdown completes in this window.
        requested = shutdown_requested()
        checked.set()
        assert release.wait(5)
        return requested

    def simulated_git(root: Path, *args: str, **kwargs: object) -> str:
        assert args == ("rev-parse", "--show-toplevel")
        return str(tmp_path)

    def register(ctx: AppContext, source: str, child: str, *, workspace: str, preserve_title: bool) -> None:
        return None

    def run(child: str, data: dict[str, object]) -> None:
        started.set()

    monkeypatch.setattr(ctx, "shutdown_requested", delayed_shutdown_check)
    monkeypatch.setattr(fork_workspaces, "git", simulated_git)
    monkeypatch.setattr(conversation_routes, "register_fork", register)
    monkeypatch.setattr(jobs, "_run", run)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(jobs.start, "source", CHILD, "entry", str(tmp_path))
            try:
                assert checked.wait(5)
                assert ctx.close() is True
            finally:
                release.set()
            with pytest.raises(ValueError, match="shutting down"):
                result.result(timeout=5)
        assert not started.is_set()
        assert jobs.workers == {}
        assert not jobs._file(CHILD).exists()
    finally:
        release.set()
        jobs.close()
        ctx.close()


def test_interrupted_receipt_is_recoverable_and_retains_extra_metadata(tmp_path: Path) -> None:
    ctx = AppContext(tmp_path / "state", start_scheduler=False)
    jobs = fork_workspaces.ForkWorkspaces(ctx, tmp_path)
    try:
        data: dict[str, object] = {
            "session_id": CHILD, "source": "source", "entry": "entry", "workspace": str(tmp_path),
            "root": str(jobs.home / CHILD), "status": "pending", "extra": {"retained": True},
        }
        write_json_atomic(jobs._file(CHILD), data)
        interrupted = jobs.status(CHILD)
        assert interrupted is not None
        assert interrupted["status"] == "error"
        assert interrupted["error"] == "Worktree setup was interrupted. Retry to continue."
        assert interrupted["extra"] == {"retained": True}
        assert jobs.status(CHILD) == interrupted
    finally:
        jobs.close()
        ctx.close()


def test_snapshot_pipeline_uses_a_separate_index_and_owned_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = AppContext(tmp_path / "state", start_scheduler=False)
    jobs = fork_workspaces.ForkWorkspaces(ctx, tmp_path)
    source = tmp_path / "repository" / "nested"
    source.mkdir(parents=True)
    jobs.home.mkdir()
    target = jobs.home / CHILD
    commands: list[tuple[Path, tuple[str, ...], dict[str, str] | None, bytes | None]] = []
    registered: list[tuple[str, str, str]] = []

    def simulated_git(
        root: Path, *args: str, env: dict[str, str] | None = None, data: bytes | None = None, timeout: int = 120,
    ) -> str:
        commands.append((root, args, env, data))
        if args == ("rev-parse", "--show-toplevel"):
            return str(source.parent)
        if args in {("rev-parse", "--verify", "HEAD"), ("rev-parse", "HEAD")}:
            return "head"
        if args == ("write-tree",):
            return "tree"
        if "commit-tree" in args:
            return "snapshot"
        if args[:2] == ("worktree", "add"):
            # Simulate only the resulting directories. No subprocess or Git
            # worktree is created, and no repository files are copied.
            (target / "nested").mkdir(parents=True)
        return ""

    def register(ctx: AppContext, source: str, child: str, *, workspace: str, preserve_title: bool) -> None:
        assert preserve_title
        registered.append((source, child, workspace))

    monkeypatch.setattr(fork_workspaces, "git", simulated_git)
    monkeypatch.setattr(conversation_routes, "register_fork", register)
    data: dict[str, object] = {
        "session_id": CHILD, "source": "source", "entry": "entry", "workspace": str(source),
        "root": str(target), "status": "pending",
    }
    try:
        jobs._run(CHILD, data)
        assert data["status"] == "ready" and data["snapshot"] == "snapshot"
        assert data["project_path"] == str(target / "nested")
        assert registered == [("source", CHILD, str(target / "nested"))]
        index_commands = [item for item in commands if item[2] is not None]
        assert [item[1][0] for item in index_commands] == ["read-tree", "add", "write-tree", "-c"]
        for _root, _args, env, _data in index_commands:
            assert env is not None and set(env) == {"GIT_INDEX_FILE"}
            index = Path(env["GIT_INDEX_FILE"])
            assert index.name == "index" and index.parent.parent == jobs.home
        assert commands[-1][1] == ("worktree", "add", "--detach", str(target), "snapshot")
        assert list(source.iterdir()) == []
        assert not (source.parent / ".git").exists()
        assert not (target / ".git").exists()
    finally:
        jobs.close()
        ctx.close()


def test_service_ownership_stays_per_context(tmp_path: Path) -> None:
    first = AppContext(tmp_path / "first", start_scheduler=False)
    second = AppContext(tmp_path / "second", start_scheduler=False)
    try:
        assert first.fork_workspaces is None and second.fork_workspaces is None
        owner = fork_workspaces.service(first)
        assert fork_workspaces.service(first) is owner
        assert second.fork_workspaces is None
        assert fork_workspaces.service(second) is not owner
    finally:
        first.close()
        second.close()


def test_close_retains_active_worker_and_fences_later_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = AppContext(tmp_path / "state", start_scheduler=False)
    jobs = fork_workspaces.ForkWorkspaces(ctx, tmp_path)
    ctx.fork_workspaces = jobs
    started, release = Event(), Event()

    def run(child: str, data: dict[str, object]) -> None:
        started.set()
        assert release.wait(5)

    def register(ctx: AppContext, source: str, child: str, *, workspace: str, preserve_title: bool) -> None:
        return None

    monkeypatch.setattr(jobs, "_run", run)
    monkeypatch.setattr(conversation_routes, "register_fork", register)
    # Existing project_path skips even the simulated repository lookup.
    write_json_atomic(jobs._file(CHILD), {
        "session_id": CHILD, "source": "source", "entry": "entry", "workspace": str(tmp_path),
        "root": str(jobs.home / CHILD), "project_path": str(tmp_path), "status": "error",
    })
    try:
        assert jobs.start("source", CHILD, "entry", str(tmp_path))["status"] == "pending"
        assert started.wait(5)
        assert jobs.start("source", CHILD, "entry", str(tmp_path))["status"] == "pending"
        assert len(jobs.workers) == 1
        assert jobs.close() is False
        assert not ctx.shutdown_requested()
        with pytest.raises(ValueError, match="shutting down"):
            jobs.start("source", CHILD, "entry", str(tmp_path))
        release.set()
        assert jobs.close() is True
    finally:
        release.set()
        jobs.close()
        ctx.close()


@pytest.mark.parametrize("destination", ["outside", "occupied"])
def test_failed_setup_preserves_unowned_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, destination: str,
) -> None:
    ctx = AppContext(tmp_path / "state", start_scheduler=False)
    jobs = fork_workspaces.ForkWorkspaces(ctx, tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "outside" if destination == "outside" else jobs.home / CHILD
    target.mkdir(parents=True)
    marker = target / "keep.txt"
    marker.write_text("user-owned", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    def simulated_git(root: Path, *args: str, **kwargs: object) -> str:
        commands.append(args)
        assert args == ("rev-parse", "--show-toplevel")
        return str(source)

    monkeypatch.setattr(fork_workspaces, "git", simulated_git)
    data: dict[str, object] = {
        "session_id": CHILD, "source": "source", "entry": "entry", "workspace": str(source),
        "root": str(target), "status": "pending", "snapshot": "snapshot",
    }
    try:
        jobs._run(CHILD, data)
        assert data["status"] == "error"
        error = data["error"]
        assert isinstance(error, str)
        assert ("outside its owned directory" if destination == "outside" else "destination is occupied") in error
        assert marker.read_text(encoding="utf-8") == "user-owned"
        assert list(target.iterdir()) == [marker]
        assert commands == [("rev-parse", "--show-toplevel")]
    finally:
        jobs.close()
        ctx.close()
