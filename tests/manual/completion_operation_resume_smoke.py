"""Stop / crash resume smoke for runtime operation effects (0.5.1).

Roadmap 0.5.1 requires deterministic crash-position tests plus one manual
stop/resume smoke. This script is that smoke entry: it runs the production
headless spine (``run_headless``), hard-kills the process mid-run the way a
crash would, and then reads the last committed runtime phase with a fresh
store -- recovery must switch on the log projection, never on missing events.

    --self-test   deterministic and offline: the parent waits until the run's
                  runtime phase reaches the writer_running phase, hard-kills the
                  process the way a crash would, checks the recovered register,
                  then resumes the same run_id to terminal. This is the release
                  gate.
    --child       internal: run one headless task against the given state
                  home. Used by --self-test; not meant to be invoked by hand.

Usage:
    python -B tests/manual/completion_operation_resume_smoke.py --self-test
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.runtime.effects import (
    PHASE_WRITER_RUNNING,
    RuntimeOperationStore,
    lane_for_run,
    operation_id_for_run,
)
from codey.runtime.reducer import reduce_session
from codey.runtime.session_log import RuntimeSessionLog
from codey.runs.details import load_run_details


SESSION = "smoke-resume"
RUN = "smoke-run-1"
WRITER_SLEEP_SECONDS = 30.0
KILL_TIMEOUT_SECONDS = 20.0


class _FakeProvider:
    name = "Smoke"

    def new_chat(self, timeout=None) -> None:
        del timeout

    def send(self, text: str, timeout=None) -> str:
        del timeout, text
        return '{"tool":"done","args":{"summary":"ok"}}'

    def close(self) -> None:
        pass


def _scripted_writer(request):
    """Stay inside the writer phase long enough to be killed there."""

    request.on_event(_writer_event())
    time.sleep(WRITER_SLEEP_SECONDS)
    raise AssertionError("the smoke process must be killed before this line")


def _resuming_writer(_request):
    from codey.agents.runner import RunResult

    return RunResult("resumed to terminal", "done", 1)


def _writer_event():
    from codey.runtime.events import RunEvent

    return RunEvent.status("[smoke] writer running")


def _run_child(state_home: Path, project: Path, stream: Path) -> None:
    from codey.agents.runner import RunResult  # noqa: F401  (used by readers)
    from codey.app.headless_runner import HeadlessRequest, run_headless

    rows: list[dict[str, object]] = []
    run_headless(
        HeadlessRequest(
            project=project,
            task="smoke: kill me mid-writer",
            provider_id="deepseek",
            max_turns=4,
            session_id=SESSION,
            run_id=RUN,
            state_home=state_home,
        ),
        emit_jsonl=rows.append,
        agent_run=_scripted_writer,
        collect_changes=lambda *_args, **_kwargs: {
            "ok": True,
            "changed_count": 0,
            "files": [],
            "diff": "",
        },
        connect_provider=lambda *_args, **_kwargs: _FakeProvider(),
    )
    # Unreachable in the kill scenario; kept so a non-crash run still leaves
    # its JSONL behind for inspection.
    stream.write_text(json.dumps(rows, indent=1), encoding="utf-8")


def _self_test() -> int:
    from codey.runs.ledger import RunLedgerStore

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        state_home = root / "state"
        project = root / "project"
        project.mkdir()

        child = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(Path(__file__).resolve()),
                "--child",
                "--state-home",
                str(state_home),
                "--project",
                str(project),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            store = RuntimeOperationStore(RuntimeSessionLog(state_home))
            deadline = time.monotonic() + KILL_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                register = store.load(SESSION, RUN)
                # Wait for the writer phase itself: killing at accepted
                # would make the smoke pass without ever entering the
                # writer. (accepted is covered by the deterministic
                # crash-position unit tests instead.)
                if register is not None and register.phase == PHASE_WRITER_RUNNING:
                    break
                if child.poll() is not None:
                    print("FAIL: child exited before entering the writer phase")
                    return 1
                time.sleep(0.05)
            else:
                print("FAIL: register never reached the writer phase")
                child.kill()
                return 1

            # Hard kill: no cleanup, no terminal commit -- exactly a crash.
            child.kill()
            child.wait(timeout=10)
        finally:
            if child.poll() is None:  # pragma: no cover - defensive cleanup
                child.kill()
                child.wait(timeout=10)

        recovered = RuntimeOperationStore(RuntimeSessionLog(state_home)).load(SESSION, RUN)
        if recovered is None:
            print("FAIL: register did not survive the crash")
            return 1
        if recovered.phase != PHASE_WRITER_RUNNING:
            print(f"FAIL: unexpected phase after crash: {recovered.phase}")
            return 1

        summary = load_run_details(
            run_ledgers=RunLedgerStore(state_home),
            run_traces=None,
            runtime_operations=RuntimeOperationStore(RuntimeSessionLog(state_home)),
            session_id=SESSION,
            run_id=RUN,
        )
        progress = [row for row in summary.rows if row.label == "Progress"]
        print("recovered register:")
        print(f"  phase        {recovered.phase}")
        print(f"  project_ref  {recovered.project_ref}")
        for row in summary.rows:
            payload = row.to_jsonable()
            print(f"  {payload['label']:<12} {payload['value']}")

        if len(progress) != 1 or progress[0].value != "Writing was interrupted":
            print("FAIL: honest progress line missing or wrong")
            return 1
        if str(project) in json.dumps(recovered.to_payload()):
            print("FAIL: raw project path leaked into the register")
            return 1
        resumed = _resume_same_run(state_home, project)
        if resumed != 0:
            return resumed
        print("ok: crash resume reports the last committed phase and resumes the same run")
        return 0


def _resume_same_run(state_home: Path, project: Path) -> int:
    from codey.app.headless_runner import HeadlessRequest, run_headless

    rows: list[dict[str, object]] = []
    result = run_headless(
        HeadlessRequest(
            project=project,
            task="smoke: resume the interrupted run",
            provider_id="deepseek",
            max_turns=4,
            session_id=SESSION,
            run_id=RUN,
            state_home=state_home,
        ),
        emit_jsonl=rows.append,
        agent_run=_resuming_writer,
        collect_changes=lambda *_args, **_kwargs: {
            "ok": True,
            "changed_count": 0,
            "files": [],
            "diff": "",
        },
        connect_provider=lambda *_args, **_kwargs: _FakeProvider(),
    )
    if result.stop_reason != "done":
        print(f"FAIL: resumed run did not finish cleanly: {result.stop_reason}")
        return 1
    store = RuntimeOperationStore(RuntimeSessionLog(state_home))
    terminal = store.load(SESSION, RUN)
    if terminal is None or terminal.phase != "terminal":
        phase = None if terminal is None else terminal.phase
        print(f"FAIL: resumed register is not terminal: {phase}")
        return 1
    projection = reduce_session(RuntimeSessionLog(state_home).read(SESSION))
    op_id = operation_id_for_run(RUN)
    operation = projection.operations.get(op_id)
    if operation is None or operation.status != "settled" or operation.outcome != "completed":
        print("FAIL: resumed runtime operation did not settle as completed")
        return 1
    if projection.lanes.get(lane_for_run(RUN)) is None:
        print("FAIL: resumed runtime lane is missing")
        return 1
    if projection.lanes[lane_for_run(RUN)].open_operation_id:
        print("FAIL: resumed runtime lane is still open")
        return 1
    started = [
        entry for entry in RuntimeSessionLog(state_home).read(SESSION)
        if entry.operation_id == op_id and entry.kind == "operation_started"
    ]
    if len(started) != 1:
        print(f"FAIL: expected one operation_started row, got {len(started)}")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="deterministic offline gate")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--state-home", type=Path, default=None)
    parser.add_argument("--project", type=Path, default=None)
    args = parser.parse_args()

    if args.self_test:
        return _self_test()
    if args.child:
        if args.state_home is None or args.project is None:
            parser.error("--child requires --state-home and --project")
        _run_child(args.state_home, args.project, args.state_home / "child.jsonl")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
