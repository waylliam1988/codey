from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

from codey.runtime.core import cancellation
from codey.storage.execution_output_store import ExecutionOutputObserver, ExecutionOutputStore


def test_real_process_output_is_visible_before_exit(tmp_path):
    store = ExecutionOutputStore(tmp_path, max_bytes=4096)
    execution_id = "execution-live"
    store.create(execution_id, session_id="session", run_id="run")
    observer = ExecutionOutputObserver(store, execution_id)
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys,time; print('ready', flush=True); time.sleep(2); print('done', flush=True)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    result: dict[str, object] = {}

    def wait_for_process():
        result["value"] = cancellation.wait_process(
            process,
            None,
            "live-output",
            5,
            capture_limit_bytes=4096,
            observer=observer,
        )

    worker = threading.Thread(target=wait_for_process)
    worker.start()
    deadline = time.monotonic() + 2
    seen = ""
    while time.monotonic() < deadline:
        snapshot = store.snapshot(execution_id)
        seen = snapshot["stdout"]["text"]
        if "ready" in seen:
            break
        time.sleep(0.02)
    assert "ready" in seen
    assert worker.is_alive()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert result["value"].returncode == 0
    settled = store.settled(execution_id, 0)
    assert settled["execution_state"] == "settled"
    observer.close()
    store.close()


def test_observer_preserves_utf8_split_across_pipe_reads(tmp_path):
    store = ExecutionOutputStore(tmp_path, max_bytes=128)
    store.create("e-utf8", session_id="s", run_id="r")
    observer = ExecutionOutputObserver(store, "e-utf8")
    raw = "汉字".encode()
    observer.offer("stdout", raw[:2])
    observer.offer("stdout", raw[2:])
    observer.close()
    assert store.snapshot("e-utf8")["stdout"]["text"] == "汉字"


def test_run_tool_uses_one_execution_identity_and_settles_snapshot(tmp_path: Path):
    from codey.toolchain.runtime import run_command_raw

    store = ExecutionOutputStore(tmp_path / "state", max_bytes=4096)
    (tmp_path / "probe.py").write_text("print('ready')\n", encoding="utf-8")
    result = run_command_raw(
        tmp_path,
        ".",
        "python -m py_compile probe.py",
        permission_profile="coding_writer",
        execution_store=store,
        session_id="s",
        run_id="r",
    )
    assert getattr(result, "execution_id", "")
    snapshot = store.snapshot(result.execution_id)
    assert snapshot["execution_state"] == "settled"
    assert snapshot["stdout"]["total_bytes"] >= 0


def test_project_run_wiring_keeps_execution_observation_without_managed_outputs(tmp_path: Path):
    from types import SimpleNamespace

    from codey.operations.project_completion_context import (
        AgentAccess,
        PersistenceAccess,
        ProjectCompletionDeps,
        ReviewAccess,
        RuntimeAccess,
        VerificationAccess,
        managed_tool_fns,
    )

    store = ExecutionOutputStore(tmp_path / "state", max_bytes=4096)
    (tmp_path / "probe.py").write_text("print('wired')\n", encoding="utf-8")
    deps = ProjectCompletionDeps(
        state=SimpleNamespace(),
        agent=AgentAccess(
            run=lambda *args, **kwargs: None,
            capture_provider_failure=lambda *args, **kwargs: None,
        ),
        verification=VerificationAccess(
            collect_changes=lambda *args, **kwargs: None,
            workspace_revisions=None,
        ),
        review=ReviewAccess(run=lambda *args, **kwargs: None),
        runtime=RuntimeAccess(),
        persistence=PersistenceAccess(execution_outputs=store),
    )

    tool_fns = managed_tool_fns(deps, session_id="s", run_id="r")
    assert tool_fns is not None
    result = tool_fns.execute_run_command(
        tmp_path,
        ".",
        "python -m py_compile probe.py",
        permission_profile="coding_writer",
        phase="writer",
    )
    execution_id = result.audit.get("execution_id")
    assert isinstance(execution_id, str) and execution_id
    assert store.snapshot(execution_id)["execution_state"] == "settled"
    store.close()
