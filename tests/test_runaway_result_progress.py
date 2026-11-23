"""New information cannot prove an identical call/result loop."""

import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from codey.agents.request import AgentRequest
from codey.agents.runaway_guard import attempt_record, should_block_or_remind
from codey.app.headless_runner import HeadlessAppContext
from codey.completion.verification_policy import VerificationCandidate
from codey.operations.kernel_progress import KernelProgress, project_check_cycle
from codey.operations.progress_comparison import ProjectCheckComparison, pytest_failure_ids, unittest_failure_ids
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.core.progress import ProgressSnapshot
from codey.types.json_value import JsonObject
from tests.support.kernel_runtime import run_agent


@pytest.mark.parametrize("tool", ["read", "read_file", "search"])
@pytest.mark.parametrize("texts", [("first", "second", "third"), ("first", "first", "changed")])
def test_same_call_with_new_output_is_not_a_proven_repeat(tool: str, texts: tuple[str, ...]) -> None:
    call = ToolCall(tool, {"path": "log.txt"})
    records = [attempt_record(call, ToolResult(call, text, ok=True), turn=turn)
               for turn, text in enumerate(texts)]

    assert not should_block_or_remind(records).block


@pytest.mark.parametrize("tool", ["read", "read_file", "search"])
def test_identical_call_and_result_still_triggers_the_guard(tool: str) -> None:
    call = ToolCall(tool, {"path": "log.txt"})
    records = [attempt_record(call, ToolResult(call, "unchanged", ok=True), turn=turn) for turn in range(3)]

    assert should_block_or_remind(records).block


def test_pytest_summary_requires_all_failure_and_error_rows() -> None:
    stdout = (
        "=== short test summary info ===\n"
        "ERROR tests/test_api.py::test_setup - fixture unavailable\n"
        "FAILED tests/test_limit.py::test_limit - assert 2 == 3\n"
        "=== 1 failed, 2 passed, 1 error in 0.77s ===\n"
    )
    assert pytest_failure_ids(stdout, 1) == (
        "ERROR tests/test_api.py::test_setup", "FAILED tests/test_limit.py::test_limit",
    )
    assert pytest_failure_ids(stdout.replace("1 failed", "2 failed"), 1) is None
    assert pytest_failure_ids(stdout.replace("ERROR ", "WARNING "), 1) is None
    assert pytest_failure_ids(stdout, 0) is None
    assert pytest_failure_ids(stdout, 125) is None
    assert pytest_failure_ids("2 passed in 0.11s", 0) == ()


def test_unittest_stderr_uses_the_actual_named_failure_and_count() -> None:
    output = """F.
======================================================================
FAIL: test_display (test_app.Names.test_display)
----------------------------------------------------------------------
AssertionError: '[ Hello, World! ]' != '[hello-world]'

----------------------------------------------------------------------
Ran 2 tests in 0.001s

FAILED (failures=1)
"""
    assert unittest_failure_ids(output, 1) == ("FAIL: test_display (test_app.Names.test_display)",)
    assert unittest_failure_ids(output.replace("failures=1", "failures=2"), 1) is None
    assert unittest_failure_ids(output.rsplit("FAILED", 1)[0], 1) is None
    assert unittest_failure_ids(output, 0) is None
    assert unittest_failure_ids("Ran 2 tests in 0.001s\n\nOK\n", 0) == ()


@pytest.mark.parametrize("change", ["same", "failure", "cwd", "fingerprint", "already_delivered", "real_edit"])
def test_check_feedback_follows_unchanged_edit_and_distinct_equal_execution(change: str) -> None:
    original = ProjectCheckComparison("python -m pytest", ".", "fingerprint-1", 1,
                                      ("FAILED tests/test_limit.py::test_limit",), "task-A", "execution-A")
    current = replace(original, task_ref="task-B", execution_ref="execution-B")
    if change == "failure":
        current = replace(current, failure_ids=("FAILED tests/test_api.py::test_limit",))
    elif change == "cwd":
        current = replace(current, cwd="subproject")
    elif change == "fingerprint":
        current = replace(current, workspace_fingerprint="fingerprint-2")
    elif change == "already_delivered":
        current = original
    initial_run = ToolResult(ToolCall("run", {}), "first receipt", ok=False)
    edit = ToolResult(ToolCall("edit", {"path": "limit.py"}), "edit", ok=True, audit={"changed": change == "real_edit"})
    accepted = ToolResult(ToolCall("run", {}), "accepted", ok=True, kind="accepted")
    polled = ToolResult(ToolCall("task_output", {"task_ref": "task-B"}), "terminal output page", ok=True)
    history = {"run": initial_run, "edit": edit, "accepted": accepted, "output": polled}

    def compare(row: ToolResult) -> ProjectCheckComparison | None:
        if row is initial_run:
            return original
        return current if row is polled else None

    cycle = project_check_cycle([polled], history, compare)
    if change == "same":
        assert cycle is not None
        feedback = cycle.feedback
        assert "left limit.py unchanged" in feedback and "FAILED tests/test_limit.py::test_limit" in feedback
        assert "execution-B" in feedback and '"task_ref":"task-B"' in feedback
    else:
        assert cycle is None


def test_verified_cycle_warns_before_stopping_and_survives_reload() -> None:
    session = TaskSession(TaskPolicy(frozenset({"control"})))
    progress = KernelProgress(2)
    run = ToolResult(ToolCall("run", {"command": "python -m pytest"}), "first receipt", ok=False)
    check = ProjectCheckComparison("python -m pytest", ".", "fingerprint", 1, ("FAILED tests.py::test_a",), "task-1", "e1")
    assert not progress.observe([run], session, batch_id="first", check_facts={0: check})
    assert progress.idle == 0
    # A new failure is progress even at nonzero exit. Revisiting an old failure
    # does not make it new, but the first proved no-op cycle still gets a warning.
    distinct = replace(check, failure_ids=("FAILED tests.py::test_b",), execution_ref="e2")
    assert not progress.observe([run], session, batch_id="second", check_facts={0: distinct})
    assert progress.idle == 0
    revisited = replace(check, execution_ref="e3")
    assert not progress.observe([run], session, batch_id="warning", check_facts={0: revisited}, check_cycle="cycle")
    assert progress.idle == 1 and progress.warning_batch_id == "warning"
    saved = ProgressSnapshot.from_payload(progress.snapshot().to_payload())
    restored = KernelProgress(2, snapshot=saved)
    assert not restored.observe([run], session, batch_id="warning", check_facts={0: revisited}, check_cycle="cycle")
    assert restored.snapshot() == saved
    repeated = replace(check, execution_ref="e4")
    assert restored.observe([run], session, batch_id="repeated", check_facts={0: repeated}, check_cycle="cycle")
    assert restored.check_cycle_repeated


def test_owned_pytest_checks_put_unchanged_edit_diagnostic_in_next_request(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "limit.py").write_text("LIMIT = 2\n", encoding="utf-8")
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (project / "test_limit.py").write_text(
        "from limit import LIMIT\n\ndef test_limit():\n    assert LIMIT == 3\n", encoding="utf-8"
    )
    check: JsonObject = {"tool": "run", "args": {"command": {"check": "project"}}}
    read: JsonObject = {"tool": "read_file", "args": {"path": "limit.py"}}

    def edit(value: int) -> JsonObject:
        return {"tool": "edit", "args": {"path": "limit.py", "replacements": [
            {"old_string": "LIMIT = 2", "new_string": f"LIMIT = {value}"},
        ]}}

    done: JsonObject = {"tool": "done", "args": {"summary": "Changed the one limit and verified it."}}
    replies = iter((check, read, edit(2), check, edit(3), check, done, done))
    prompts: list[str] = []

    class Provider:
        name = "scripted"
        location = "in-process"

        def new_chat(self, timeout: float | None = None) -> None:
            pass

        def send(self, text: str, timeout: float | None = None) -> str:
            prompts.append(text)
            return json.dumps(next(replies))

        def close(self) -> None:
            pass

    with closing(HeadlessAppContext(tmp_path / "state", port=0, emit_jsonl=lambda _event: None)) as state:
        result = run_agent(AgentRequest(
            Provider(), project, "Fix the limit in limit.py and verify the change with pytest.",
            provider_id="test", durable=state.durable, managed_outputs=state.managed_outputs,
            workspace_revision_store=state.workspace_revisions, runtime_mutations=state.runtime_mutations,
            verification_candidates=(VerificationCandidate("python -m pytest"),), max_turns=8,
        ))

    assert result.stop_reason == "done", result
    assert "left limit.py unchanged" in prompts[4]
    assert "FAILED test_limit.py::test_limit" in prompts[4]
    assert '"tool":"task_output"' in prompts[4]
    assert (project / "limit.py").read_text(encoding="utf-8") == "LIMIT = 3\n"
