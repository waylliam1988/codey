"""Parent completion and cancellation own every admitted tool execution."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from codey.codemode.runtime import NodeWorkerRuntime
from codey.codemode.types import CodeModeLimits, ToolCatalogEntry
from codey.runtime.core import cancellation


class _WaitForChild:
    def __init__(self, started):
        self.started = started

    def store(self, _key, _value):
        assert self.started.wait(3)


def test_parent_return_waits_for_unawaited_host_call_and_rejects_success():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def invoke(_name, _args):
        started.set()
        assert release.wait(5)
        finished.set()
        return "finished"

    with ThreadPoolExecutor(max_workers=1) as pool:
        parent = pool.submit(
            NodeWorkerRuntime().execute,
            "void tools.read_file({path:'a.txt'}); await store('started',true); return 'too early';",
            catalog=(ToolCatalogEntry("read_file", "read"),), invoke=invoke, state_store=_WaitForChild(started),
        )
        try:
            assert started.wait(3)
            with pytest.raises(TimeoutError):
                parent.result(timeout=0.15)
        finally:
            release.set()
        result = parent.result(timeout=3)
    assert finished.is_set()
    assert not result.ok
    assert result.error_code == "unawaited_calls"


@pytest.mark.parametrize("source", [
    "void tools.read_file({path:'a.txt'}); await store('started',true); throw new Error('parent failed');",
    "await tools.read_file({path:'a.txt'}); return 1;",
])
def test_parent_failure_or_deadline_cancels_and_joins_child(source):
    started, finished = threading.Event(), threading.Event()

    def invoke(_name, _args):
        started.set()
        try:
            cancellation.wait(10)
        finally:
            finished.set()

    result = NodeWorkerRuntime().execute(
        source, catalog=(ToolCatalogEntry("read_file", "read"),), invoke=invoke,
        limits=CodeModeLimits(timeout_ms=1500), state_store=_WaitForChild(started),
    )
    assert started.is_set()
    assert finished.is_set(), "parent returned while its child was still running"
    assert not result.ok


def test_stop_reaches_codemode_child_and_parent_joins_it():
    stop, started, finished = threading.Event(), threading.Event(), threading.Event()

    def invoke(_name, _args):
        started.set()
        try:
            cancellation.wait(10)
        finally:
            finished.set()

    def run():
        with cancellation.scope(stop):
            return NodeWorkerRuntime().execute(
                "return await tools.read_file({path:'a.txt'});",
                catalog=(ToolCatalogEntry("read_file", "read"),), invoke=invoke,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        parent = pool.submit(run)
        try:
            assert started.wait(3)
            stop.set()
            with pytest.raises(cancellation.TaskCancelled):
                parent.result(timeout=3)
        finally:
            stop.set()
    assert finished.is_set()


def test_codemode_inherits_the_earliest_task_deadline():
    import time

    finished = threading.Event()

    def invoke(_name, _args):
        try:
            cancellation.wait(10)
        finally:
            finished.set()

    with cancellation.deadline_scope(time.monotonic() + 2):
        result = NodeWorkerRuntime().execute(
            "return await tools.read_file({path:'a.txt'});",
            catalog=(ToolCatalogEntry("read_file", "read"),), invoke=invoke,
        )
    assert not result.ok
    assert result.error_code == "timeout"
    assert finished.is_set()


def test_leaf_dispatch_holds_exclusivity_through_receipt_commit():
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult

    settling, release, read_started = threading.Event(), threading.Event(), threading.Event()
    session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.read", "project.write", "project.verify"})))
    snapshot = build_turn_snapshot(session)

    class Sink:
        def has_unsettled(self, _identity):
            return False

        def begin_turn(self, *_args, **_kwargs):
            pass

        def settle(self, _identity, _ok, *, result, exit_code):
            if result.call.name == "run":
                settling.set()
                assert release.wait(5)

    def read(call):
        read_started.set()
        return ToolResult(ok=True, call=call, model_text="read")

    def run(call):
        return ToolResult(ok=True, call=call, model_text="verified", audit={"exit_code": 0})

    def execute(name, turn):
        args = {"command": "python -m unittest", "path": "."} if name == "run" else {"path": "a.py"}
        return execute_turn(session, [ToolCall(name, args)], snapshot=snapshot, turn=turn,
                            run_id="r", intent_sink=Sink(), executors={"run": run, "read_file": read})

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(execute, "run", 1)
        try:
            if not settling.wait(3):
                writer.result(timeout=0.1)
            assert settling.wait(3)
            reader = pool.submit(execute, "read_file", 2)
            assert not read_started.wait(0.15), "reader ran before the writer receipt committed"
        finally:
            release.set()
        assert writer.result(timeout=3)[0].ok
        assert reader.result(timeout=3)[0].ok
    assert len(session.executed) == 2


def test_queued_child_stop_settles_once_without_invoking_the_tool():
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult

    session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"})))
    snapshot = build_turn_snapshot(session)
    stop, entering = threading.Event(), threading.Event()
    invoked = []

    def run():
        entering.set()
        return execute_turn(
            session, [ToolCall("read_file", {"path": "a.txt"})], snapshot=snapshot, run_id="r", turn=1,
            stop_flag=stop, executors={"read_file": lambda call: invoked.append(call)
                                     or ToolResult(ok=True, call=call, model_text="unexpected")},
        )

    # The same session admission is used by normal calls and CodeMode children.
    with ThreadPoolExecutor(max_workers=1) as pool, session.tool_dispatch.admit(parallel=False):
        waiting = pool.submit(run)
        assert entering.wait(3)
        stop.set()
        results = waiting.result(timeout=3)
    assert not invoked
    assert not results[0].ok
    assert len(session.executed) == 1


def test_stop_terminates_a_process_started_by_a_codemode_child(tmp_path, monkeypatch):
    import sys

    stop, started, finished = threading.Event(), threading.Event(), threading.Event()
    processes = []
    original = cancellation.start_process

    def start(*args, **kwargs):
        proc, owner = original(*args, **kwargs)
        processes.append(proc)
        started.set()
        return proc, owner

    monkeypatch.setattr(cancellation, "start_process", start)

    def invoke(_name, _args):
        try:
            cancellation.run_process([sys.executable, "-c", "import time; time.sleep(30)"],
                                     cwd=tmp_path, timeout=40, capture_limit_bytes=1024)
        finally:
            finished.set()

    def run():
        with cancellation.scope(stop):
            return NodeWorkerRuntime().execute(
                "return await tools.run({});", catalog=(ToolCatalogEntry("run", "run"),), invoke=invoke,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        parent = pool.submit(run)
        try:
            assert started.wait(3)
            stop.set()
            with pytest.raises(cancellation.TaskCancelled):
                parent.result(timeout=5)
        finally:
            stop.set()
    assert finished.is_set()
    assert processes and all(proc.poll() is not None for proc in processes)


def test_cancelled_replay_preserves_committed_result_without_reexecution():
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult

    session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.read"})))
    snapshot = build_turn_snapshot(session)
    call = ToolCall("read_file", {"path": "a.txt"})
    invoked = []

    def read(item):
        invoked.append(item)
        return ToolResult(ok=True, call=item, model_text="committed")

    kwargs = dict(snapshot=snapshot, run_id="r", turn=1, executors={"read_file": read})
    assert execute_turn(session, [call], **kwargs)[0].ok
    committed = dict(session.executed)
    stop = threading.Event()
    stop.set()
    assert execute_turn(session, [call], stop_flag=stop, **kwargs)[0].ok
    assert session.executed == committed
    assert invoked == [call]


@pytest.mark.parametrize("failure", ["executor", "request"])
def test_worker_acquisition_failure_closes_the_process_and_pipes(monkeypatch, failure):
    from codey.codemode import runtime

    processes = []
    original = runtime.subprocess.Popen

    def spawn(*args, **kwargs):
        proc = original(*args, **kwargs)
        processes.append(proc)
        return proc

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected acquisition failure")

    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    if failure == "executor":
        monkeypatch.setattr(runtime, "ThreadPoolExecutor", fail)
    else:
        monkeypatch.setattr(NodeWorkerRuntime, "_write_line", fail)
    with pytest.raises(RuntimeError, match="injected acquisition failure"):
        NodeWorkerRuntime().execute("return 1;", catalog=(), invoke=lambda *_: None)
    assert len(processes) == 1
    proc = processes[0]
    assert proc.poll() is not None
    assert proc.stdin.closed and proc.stdout.closed and proc.stderr.closed
