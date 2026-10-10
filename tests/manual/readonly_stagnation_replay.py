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
from unittest import mock

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
) -> dict[str, object]:
    from codey.app import server
    from codey.operations import project_adapter
    from codey.operations.task_entry import run_task_submission
    from codey.task.model import TaskSubmission
    from tests.test_project_completion_flow_enforcement import _runner

    project = run_dir / "project"
    state_dir = run_dir / "state"
    project.mkdir(parents=True, exist_ok=True)
    (project / "app.py").write_text(APP, encoding="utf-8")
    (project / "test_app.py").write_text(
        TESTS if verification_passes else FAILING_TESTS,
        encoding="utf-8",
    )
    baseline = _snapshot(project)
    state = server.AppContext(state_dir)
    command = verification_command or COMMAND

    def on_call(number: int) -> None:
        if mutate_after_check and number == 2:
            (project / "app.py").write_text(APP + "# changed after verification\n", encoding="utf-8")
        if cancel_after_initial and number == 5:
            state.run_registry.stop_flag.set()

    provider = RepeatingReadProvider(
        submit_on_recovery=submit_on_recovery,
        repeat_check_on_recovery=repeat_check_on_recovery,
        command=command,
        on_call=on_call,
    )
    task_text = TASK.replace(COMMAND, command)
    events: list[dict[str, object]] = []
    calls: list[str] = []
    profiles: list[str] = []
    task_kinds: list[str] = []
    conversation_modes: list[str] = []

    def observe(event, downstream, stop_flag):
        call = getattr(event, "call", None)
        outcome = getattr(event, "outcome", None)
        if call is not None and outcome is not None:
            events.append({
                "tool": call.name,
                "args": call.args,
                "ok": outcome.ok,
                "exit_code": outcome.exit_code,
                "error_code": outcome.error_code,
                "model_text": outcome.model_text,
                "execution_disposition": (getattr(outcome, "audit", None) or {}).get(
                    "execution_disposition", ""
                ),
                "audit": getattr(outcome, "audit", None),
            })
        downstream(event)

    def writer(request):
        calls.append(request.task)
        profiles.append(request.permission_profile)
        task_kinds.append(str(getattr(request.task_session, "task_kind", "")))
        wrapped = replace(
            request,
            provider=provider,
            fresh_chat=True,
            on_event=lambda event: observe(event, request.on_event, request.stop_flag),
        )
        result = project_adapter.run(wrapped)
        conversation_modes.append(str(getattr(request.conversation.snapshot, "mode", "")))
        return result

    def collect_changes(*_args: object, **_kwargs: object) -> dict[str, object]:
        current = _snapshot(project)
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

    runner = _runner(state, writer)
    runner = replace(
        runner,
        collect_changes=collect_changes,
        run_review=mock.Mock(return_value=None),
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
    operation = (
        state.runtime_operations.load("readonly-stagnation", terminal["run_id"])
        if isinstance(terminal, dict) and state.runtime_operations is not None
        else None
    )
    operation_payload = operation.to_payload() if operation is not None else None
    receipt = terminal.get("receipt") if isinstance(terminal, dict) else None
    verification = receipt.get("verification") if isinstance(receipt, dict) else None
    report = {
        "task": task_text,
        "submit_on_recovery": submit_on_recovery,
        "terminal": terminal,
        "provider_calls": provider.calls,
        "writer_submissions": len(calls),
        "permission_profiles": profiles,
        "task_kinds": task_kinds,
        "conversation_modes": conversation_modes,
        "recovery_prompt_seen": any(READONLY_FOLLOWUP_MARKER in request for request in provider.requests[5:]),
        "events": events,
        "successful_verification_runs": sum(
            event["tool"] == "run" and event["ok"] is True and event["exit_code"] == 0
            for event in events
        ),
        "verification_run_attempts": sum(
            event["tool"] == "run"
            and event.get("execution_disposition") != "denied_before_execution"
            for event in events
        ),
        "physical_verification_executions": sum(
            event["tool"] == "run"
            and isinstance(event.get("audit"), dict)
            and isinstance(event["audit"].get("command_started_at"), str)
            and isinstance(event["audit"].get("command_finished_at"), str)
            for event in events
        ),
        "denied_verification_requests": sum(
            event["tool"] == "run"
            and event.get("execution_disposition") == "denied_before_execution"
            for event in events
        ),
        "verification_exit_codes": [
            event["exit_code"] for event in events
            if event["tool"] == "run" and event.get("execution_disposition") != "denied_before_execution"
        ],
        "final_files": _snapshot(project),
        "files_unchanged": _snapshot(project) == baseline,
        "recovery_attempted": bool(
            operation_payload.get("candidate_validation_attempted")
        ) if isinstance(operation_payload, dict) else None,
        "operation": operation_payload,
        "proof": (
            operation_payload.get("completion_proof_status")
            if isinstance(operation_payload, dict) else None
        ),
        "proof_refs": verification.get("proof_refs", []) if isinstance(verification, dict) else [],
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    report = replay(run_dir)
    (run_dir / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "stop_reason": (report.get("terminal") or {}).get("stop_reason"),
        "provider_calls": report["provider_calls"],
        "successful_verification_runs": report["successful_verification_runs"],
        "files_unchanged": report["files_unchanged"],
        "artifact": str(run_dir / "result.json"),
    }, ensure_ascii=False), flush=True)
    terminal = report.get("terminal") or {}
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
