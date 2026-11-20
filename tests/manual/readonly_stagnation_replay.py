"""Replay a read-only verified task that stalls after repeating reads.

This is a deterministic pre-production experiment. It uses the real project
adapter, kernel execution and completion gate, while the provider emits a fixed
sequence instead of calling a model. The provider can either submit ``done``
on the one bounded read-only recovery turn or keep reading and prove that the
runtime stops safely.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest import mock

from codey.agents.request import AgentRequest
from codey.providers.diagnostics import ProviderFailure
from codey.runtime.core.run_result import RunResult
from codey.runtime.observe.events import RunEvent
from codey.types.json_value import JsonObject, decode_json_object, require_json_string
from codey.workspace.changes import ChangeTracker

COMMAND = "python -m unittest discover"
TASK = (
    "Verify app.py already satisfies the requested behavior. Do not change any file. "
    f"Run {COMMAND} once and finish only after the tests pass."
)
APP = "def answer():\n    return 'ok'\n"
TESTS = (
    "import unittest\n"
    "from app import answer\n\n"
    "class AnswerTests(unittest.TestCase):\n"
    "    def test_answer(self):\n"
    "        self.assertEqual(answer(), 'ok')\n"
)
FAILING_TESTS = TESTS.replace("self.assertEqual(answer(), 'ok')", "self.assertEqual(answer(), 'wrong')")


READONLY_FOLLOWUP_MARKER = "Submit the read-only verification result"


class RepeatingReadProvider:
    name = "fixed readonly stagnation replay"
    location = "fixture:readonly-stagnation"

    def __init__(self, *, submit_on_recovery: bool = False,
                 repeat_check_on_recovery: bool = False,
                 command: str = COMMAND,
                 on_call: Callable[[int], None] | None = None) -> None:
        self.calls = 0
        self.requests: list[str] = []
        self.submit_on_recovery = submit_on_recovery
        self.repeat_check_on_recovery = repeat_check_on_recovery
        self.command = command
        self.on_call = on_call

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout

    def close(self) -> None:
        return None

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        self.calls += 1
        self.requests.append(text)
        if self.on_call is not None:
            self.on_call(self.calls)
        if self.calls == 1 or (self.calls == 6 and self.repeat_check_on_recovery):
            action = {"tool": "run", "args": {"command": self.command, "path": "."}}
        elif (
            (self.calls == 7 and self.repeat_check_on_recovery and self.submit_on_recovery)
            or (self.calls == 6 and self.submit_on_recovery)
        ):
            action = {"tool": "done", "args": {"summary": "The existing verification passed."}}
        else:
            path = "app.py" if self.calls % 2 else "test_app.py"
            action = {"tool": "read_file", "args": {"path": path}}
        return json.dumps(action)


def _snapshot(project: Path) -> dict[str, str]:
    return {
        path.relative_to(project).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(project.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def replay(
    run_dir: Path,
    *,
    submit_on_recovery: bool = False,
    repeat_check_on_recovery: bool = False,
    verification_passes: bool = True,
    verification_command: str | None = None,
    max_turns: int = 20,
    project_changes_required: bool = False,
    mutate_after_check: bool = False,
    cancel_after_initial: bool = False,
) -> JsonObject:
    from codey.app.context import AppContext
    from codey.operations import project_adapter
    from codey.operations.task_entry import run_task_submission
    from codey.operations.task_run import TaskRunDeps
    from codey.providers.diagnostics import capture_provider_failure
    from codey.task.model import TaskSubmission

    project = run_dir / "project"
    state_dir = run_dir / "state"
    project.mkdir(parents=True, exist_ok=True)
    (project / "app.py").write_text(APP, encoding="utf-8")
    counter = run_dir / "verification-executions.txt"
    counted_tests = (TESTS if verification_passes else FAILING_TESTS) + (
        "\nfrom pathlib import Path\n"
        f"counter = Path({str(counter)!r})\n"
        "counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else '1')\n"
    )
    (project / "test_app.py").write_text(
        counted_tests,
        encoding="utf-8",
    )
    baseline = _snapshot(project)
    state = AppContext(state_dir)
    command = verification_command or COMMAND

    def on_call(number: int) -> None:
        if mutate_after_check and number == 2:
            (project / "app.py").write_text(APP + "# changed after verification\n", encoding="utf-8")
        if cancel_after_initial and number == 5:
            # Stop the active run lane; the global admission flag is only for
            # a stop received before a run has been reserved.
            state.run_registry.request_stop()

    provider = RepeatingReadProvider(
        submit_on_recovery=submit_on_recovery,
        repeat_check_on_recovery=repeat_check_on_recovery,
        command=command,
        on_call=on_call,
    )
    task_text = TASK.replace(COMMAND, command)
    events: list[JsonObject] = []
    calls: list[str] = []
    profiles: list[str] = []
    task_kinds: list[str] = []
    conversation_modes: list[str] = []

    def observe(event: RunEvent, downstream: Callable[[RunEvent], None], stop_flag: Event | None) -> None:
        call = event.call
        outcome = event.outcome
        if call is not None and outcome is not None:
            events.append({
                "tool": call.name,
                "args": call.args,
                "ok": outcome.ok,
                "exit_code": outcome.exit_code,
                "error_code": outcome.error_code,
                "model_text": outcome.model_text,
                "execution_disposition": str(outcome.audit.get(
                    "execution_disposition", ""
                )),
                "audit": decode_json_object(outcome.audit),
            })
        downstream(event)

    def writer(request: AgentRequest) -> RunResult:
        from codey.operations.task_session import TaskSession

        calls.append(request.task)
        profiles.append(request.permission_profile)
        task_kinds.append(request.task_session.task_kind if isinstance(request.task_session, TaskSession) else "")
        wrapped = replace(
            request,
            provider=provider,
            fresh_chat=True,
            on_event=lambda event: observe(event, request.on_event, request.stop_flag),
        )
        result = project_adapter.run(wrapped)
        conversation_modes.append(request.conversation.snapshot.mode if request.conversation is not None else "")
        return result

    def collect_changes(project: str | Path, tracker: ChangeTracker | None) -> dict[str, object]:
        current = _snapshot(Path(project))
        changed = sorted(
            path for path in set(baseline) | set(current)
            if baseline.get(path) != current.get(path)
        )
        return {
            "ok": True,
            "changed_count": len(changed),
            "files": [{"path": path, "status": "modified"} for path in changed],
            "diff": "",
            "mode": "git",
        }

    def capture_failure(*, model: str, action: str, page: object, error: BaseException) -> ProviderFailure:
        return capture_provider_failure(model=model, action=action, page=page, error=error)

    def review(**kwargs: object) -> None:
        return None

    runner = TaskRunDeps.from_submission_stores(
        state=state, stores=state.task_submission_stores, agent_run=writer,
        collect_changes=collect_changes,
        run_review=review,
        capture_provider_failure=capture_failure,
        is_git_repository=lambda root: True,
    )
    with mock.patch.object(state, "get_provider", return_value=provider):
        run_task_submission(
            runner,
            TaskSubmission(
                "readonly-stagnation",
                str(project),
                task_text,
                max_turns,
                False,
                "deepseek",
                intent="project",
                project_changes_required=project_changes_required,
            ),
        )

    terminal = state.run_registry.last_terminal_event()
    assert terminal is not None
    operation = (
        state.runtime_operations.load("readonly-stagnation", require_json_string(terminal["run_id"], "run_id"))
        if state.runtime_operations is not None
        else None
    )
    operation_payload = operation.to_payload() if operation is not None else None
    receipt = decode_json_object(terminal["receipt"])
    verification = decode_json_object(receipt["verification"])
    report: dict[str, object] = {
        "task": task_text,
        "submit_on_recovery": submit_on_recovery,
        "terminal": terminal,
        "provider_calls": provider.calls,
        "writer_submissions": len(calls),
        "permission_profiles": profiles,
        "task_kinds": task_kinds,
        "conversation_modes": conversation_modes,
        "recovery_prompt_seen": any(READONLY_FOLLOWUP_MARKER in request for request in provider.requests[5:]),
        "events": list(events),
        "successful_verification_runs": sum(
            event["tool"] == "run" and event["ok"] is True and event["exit_code"] == 0
            for event in events
        ),
        "verification_run_attempts": sum(
            event["tool"] == "run"
            and event.get("execution_disposition") != "denied_before_execution"
            for event in events
        ),
        "physical_verification_executions": int(counter.read_text()) if counter.exists() else 0,
        "denied_verification_requests": sum(
            event["tool"] == "run"
            and event.get("execution_disposition") == "denied_before_execution"
            for event in events
        ),
        "verification_exit_codes": [
            event["exit_code"] for event in events
            if event["tool"] == "run" and event.get("execution_disposition") != "denied_before_execution"
        ],
        "final_files": dict(_snapshot(project)),
        "files_unchanged": _snapshot(project) == baseline,
        "recovery_attempted": bool(
            operation_payload.get("candidate_validation_attempted")
        ) if isinstance(operation_payload, dict) else None,
        "operation": decode_json_object(operation_payload) if operation_payload is not None else None,
        "proof": (
            operation_payload.get("completion_proof_status")
            if isinstance(operation_payload, dict) else None
        ),
        "proof_refs": verification.get("proof_refs", []) if isinstance(verification, dict) else [],
    }
    state.close()
    return decode_json_object(report)


class ReplayArguments(argparse.Namespace):
    run_dir: Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(namespace=ReplayArguments())
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    report = replay(run_dir)
    terminal = decode_json_object(report["terminal"])
    (run_dir / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary: JsonObject = {
        "stop_reason": terminal.get("stop_reason"),
        "provider_calls": report["provider_calls"],
        "successful_verification_runs": report["successful_verification_runs"],
        "files_unchanged": report["files_unchanged"],
        "artifact": str(run_dir / "result.json"),
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if (
        report["successful_verification_runs"] == 1
        and report["files_unchanged"] is True
        and (
            (not report["submit_on_recovery"] and terminal.get("stop_reason") in {"no_progress", "max_turns"})
            or (report["submit_on_recovery"] and terminal.get("stop_reason") == "done")
        )
    ) else 2


if __name__ == "__main__":
    raise SystemExit(main())
