"""Project-completion edit-integrity wiring (0.5.0).

The monitor must observe the real production completion path: the run's
decision, proof diagnostics, receipt trust, trace rows, and the project
facts gate all read one observation. The recorded Qwen/MiMo
``modified_test_fixture`` failure is replayed through the scripted writer
end to end.
"""

from __future__ import annotations

import difflib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.app import server
from codey.agents.request import AgentRequest
from codey.agents.runner import RunResult
from codey.completion import engine as completion_engine_module
from codey.completion.edit_integrity import observe_edit_integrity
from codey.runs.ledger_projection import event_with_projected_receipt
from codey.runs.receipt import build_task_receipt
from codey.runtime.events import RunEvent
from codey.runtime.models import ToolCall
from codey.task.model import TaskSubmission
from codey.operations.task_entry import TaskRunDeps, run_task_submission
from codey.toolchain.runtime import ToolOutcome


def _edit(path: str = "tests/test_mod.py") -> RunEvent:
    return RunEvent.tool_finished(
        1,
        ToolCall("edit", {"path": path, "old_string": "1", "new_string": "2"}),
        ToolOutcome("edited", True, changed=True),
    )


def _run_event(ok: bool) -> RunEvent:
    outcome = (
        ToolOutcome("all passed", True, exit_code=0)
        if ok
        else ToolOutcome("1 failed", False, exit_code=1)
    )
    return RunEvent.tool_finished(
        2,
        ToolCall("run", {"command": "python -m pytest", "path": "."}),
        outcome,
    )


def _changes(*files: str) -> dict:
    return {
        "ok": True,
        "changed_count": len(files),
        "files": [{"path": item, "status": "modified"} for item in files],
        "diff": _TEST_TAMPER_DIFF if "tests/test_mod.py" in files else _CLEAN_DIFF,
        "mode": "git",
    }


_TEST_BODY = "def test_value():\n    import redis\n    assert mod.VALUE == 2\n"
_TAMPERED_BODY = "def test_value():\n    assert mod.VALUE == 2\n"
_TEST_TAMPER_DIFF = "".join(difflib.unified_diff(
    _TEST_BODY.splitlines(keepends=True),
    _TAMPERED_BODY.splitlines(keepends=True),
    fromfile="a/tests/test_mod.py",
    tofile="b/tests/test_mod.py",
))
_CLEAN_DIFF = "diff --git a/src/mod.py b/src/mod.py\n--- a/src/mod.py\n+++ b/src/mod.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"


class ScriptedWriter:
    def __init__(self, *steps: tuple[list[RunEvent], RunResult]) -> None:
        self.steps = list(steps)
        self.calls: list[dict] = []

    def __call__(self, request: AgentRequest) -> RunResult:
        self.calls.append({"task": request.task, "request": request})
        events, result = self.steps.pop(0)
        for event in events:
            request.on_event(event)
        return result


class _Provider:
    name = "DeepSeek Web"
    location = "https://chat.deepseek.com/"

    def new_chat(self, timeout=None) -> None:
        del timeout

    def send(self, text: str, timeout=None) -> str:
        del timeout, text
        return '{"tool":"done","args":{"summary":"ok"}}'

    def close(self) -> None:
        pass


def _runner(state, writer: ScriptedWriter, files: tuple[str, ...]) -> TaskRunDeps:
    return TaskRunDeps(state=state,
        agent_run=writer,
        collect_changes=mock.Mock(side_effect=lambda *_a, **_k: _changes(*files)),
        run_review=mock.Mock(return_value=None),
        capture_provider_failure=server.capture_provider_failure,
        project_facts=state.project_facts,
        work_checkpoints=state.work_checkpoints,
        workspace_revisions=state.workspace_revisions,
        run_ledgers=state.run_ledgers,
        run_traces=state.run_traces,
        evidence_ledgers=state.evidence_ledgers,
        managed_outputs=state.managed_outputs,
        knowledge_store=state.knowledge_store,
        is_git_repository=lambda _project: True,
    )


def _runner_with_changes(state, writer: ScriptedWriter, collected: list[dict]) -> TaskRunDeps:
    """Runner whose successive change collections come from ``collected``."""

    return TaskRunDeps(state=state,
        agent_run=writer,
        collect_changes=mock.Mock(side_effect=lambda *_a, **_k: (
            collected.pop(0) if collected else dict(collected[-1])
        )),
        run_review=mock.Mock(return_value=None),
        capture_provider_failure=server.capture_provider_failure,
        project_facts=state.project_facts,
        work_checkpoints=state.work_checkpoints,
        workspace_revisions=state.workspace_revisions,
        run_ledgers=state.run_ledgers,
        run_traces=state.run_traces,
        evidence_ledgers=state.evidence_ledgers,
        managed_outputs=state.managed_outputs,
        knowledge_store=state.knowledge_store,
        is_git_repository=lambda _project: True,
    )


def _run(runner: TaskRunDeps, state, project: Path, task: str) -> dict:
    with mock.patch.object(state, "get_provider", return_value=_Provider()):
        run_task_submission(runner, TaskSubmission(
            "s-integrity",
            str(project),
            task,
            6,
            False,
            "deepseek",
            intent="project",
        ))
    return dict(state.run_registry.last_terminal_event())


def _trace_payload(state) -> dict:
    assert state.run_traces is not None
    path = state.run_traces.path_for("s-integrity", state.run_registry.last_terminal_event()["run_id"])
    return json.loads(path.read_text(encoding="utf-8"))


class TamperedFixtureRunTests(unittest.TestCase):
    def test_tampered_fixture_yields_needs_review_receipt_and_trace_row(self) -> None:
        # The recorded Qwen/MiMo failure: only the test file changes, the
        # check passes green, the model claims done.
        writer = ScriptedWriter((
            [_edit("tests/test_mod.py"), _run_event(True)],
            RunResult("trust me", "done", 2),
        ))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = Path(td) / "project"
            (project / "src").mkdir(parents=True)
            (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
            state = server.AppContext(Path(td) / "state")
            event = _run(
                _runner(state, writer, files=("tests/test_mod.py",)),
                state,
                project,
                "Change src/mod.py VALUE from 1 to 2 and verify",
            )

            self.assertEqual(event["stop_reason"], "done")
            receipt = event["receipt"]
            self.assertEqual(receipt["schema_version"], 1)
            self.assertEqual(receipt["verification"]["trust"], "needs_review")
            self.assertEqual(receipt["display"]["summary"], "1 file changed · checks need review")
            self.assertEqual(
                receipt["display"]["detail"],
                "Test changes may have weakened verification",
            )
            self.assertEqual(receipt["integrity"]["status"], "suspicious")
            self.assertEqual(receipt["integrity"]["severity"], "high")

            manifest = _trace_payload(state)
            rows = manifest["completion_edit_integrity"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "suspicious")
            self.assertEqual(rows[0]["severity"], "high")
            self.assertEqual(rows[0]["affected_paths"], ["tests/test_mod.py"])
            # The final proof names the observation as its diagnostic.
            proofs = manifest["completion_proofs"]
            self.assertEqual(proofs[-1]["diagnostic_refs"], [rows[0]["observation_ref"]])

    def test_high_suspicious_blocks_project_facts_and_memory(self) -> None:
        writer = ScriptedWriter((
            [_edit("tests/test_mod.py"), _run_event(True)],
            RunResult("trust me", "done", 2),
        ))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = Path(td) / "project"
            (project / "src").mkdir(parents=True)
            (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
            state = server.AppContext(Path(td) / "state")
            runner = _runner(state, writer, files=("tests/test_mod.py",))
            with mock.patch(
                "codey.operations.project_completion_flow.record_project_memory",
                autospec=True,
            ) as memory:
                _run(
                    runner,
                    state,
                    project,
                    "Change src/mod.py VALUE from 1 to 2 and verify",
                )

            facts = state.project_facts.load(str(project))
            self.assertEqual(facts.successful_changes, ())
            # High suspicious also keeps the run out of project memory.
            memory.assert_not_called()


class CleanRunTests(unittest.TestCase):
    def test_clean_source_fix_stays_trusted_and_writes_facts(self) -> None:
        writer = ScriptedWriter((
            [_edit("src/mod.py"), _run_event(True)],
            RunResult("implemented", "done", 2),
        ))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = Path(td) / "project"
            (project / "src").mkdir(parents=True)
            (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
            state = server.AppContext(Path(td) / "state")
            event = _run(
                _runner(state, writer, files=("src/mod.py",)),
                state,
                project,
                "Change src/mod.py VALUE from 1 to 2 and verify",
            )

            self.assertEqual(event["stop_reason"], "done")
            receipt = event["receipt"]
            self.assertEqual(receipt["verification"]["trust"], "trusted")
            self.assertEqual(receipt["display"]["summary"], "1 file changed · checks passed")
            self.assertEqual(receipt["display"]["detail"], "")
            manifest = _trace_payload(state)
            rows = manifest["completion_edit_integrity"]
            self.assertEqual(rows[0]["status"], "clean")
            self.assertEqual(proofs_last_diagnostic(manifest), [])

            facts = state.project_facts.load(str(project))
            self.assertEqual(len(facts.successful_changes), 1)
            self.assertIn("checks passed", facts.successful_changes[0].receipt)


class MissingDiffRunTests(unittest.TestCase):
    def test_changed_paths_without_diff_are_limited_and_do_not_write_facts(self) -> None:
        writer = ScriptedWriter((
            [_edit("src/mod.py"), _run_event(True)],
            RunResult("implemented", "done", 2),
        ))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = Path(td) / "project"
            (project / "src").mkdir(parents=True)
            (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
            state = server.AppContext(Path(td) / "state")
            missing_diff = _changes("src/mod.py")
            missing_diff["diff"] = ""
            event = _run(
                _runner_with_changes(
                    state,
                    writer,
                    [dict(missing_diff), dict(missing_diff)],
                ),
                state,
                project,
                "Change src/mod.py VALUE from 1 to 2 and verify",
            )

            self.assertEqual(event["stop_reason"], "done")
            receipt = event["receipt"]
            self.assertEqual(receipt["verification"]["trust"], "limited")
            self.assertEqual(
                receipt["display"]["summary"],
                "1 file changed · verification limited",
            )
            self.assertEqual(receipt["integrity"]["status"], "unobserved")
            self.assertIn("diff_unavailable", receipt["integrity"]["reason_codes"])
            facts = state.project_facts.load(str(project))
            self.assertEqual(facts.successful_changes, ())


def proofs_last_diagnostic(manifest: dict) -> list:
    return manifest["completion_proofs"][-1].get("diagnostic_refs", [])


class RepairRoundTests(unittest.TestCase):
    """The integrity observation must ride the same snapshot as the final
    decision: a diff captured before the repair round is stale evidence."""

    def test_tamper_introduced_during_repair_yields_needs_review(self) -> None:
        writer = ScriptedWriter(
            ([_edit("src/mod.py"), _run_event(False)], RunResult("done?", "done", 3)),
            ([_edit("tests/test_mod.py"), _run_event(True)], RunResult("fixed", "done", 2)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = Path(td) / "project"
            (project / "src").mkdir(parents=True)
            (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
            state = server.AppContext(Path(td) / "state")
            event = _run(
                _runner_with_changes(
                    state,
                    writer,
                    [_changes("src/mod.py"), _changes("tests/test_mod.py")],
                ),
                state,
                project,
                "Change src/mod.py VALUE from 1 to 2 and verify",
            )

            self.assertEqual(event["stop_reason"], "done")
            receipt = event["receipt"]
            # The tampered diff arrived WITH the repair round; the final
            # receipt must read it, not the pre-repair clean diff.
            self.assertEqual(receipt["verification"]["trust"], "needs_review")
            self.assertEqual(
                receipt["display"]["summary"],
                "1 file changed · checks need review",
            )
            manifest = _trace_payload(state)
            rows = manifest["completion_edit_integrity"]
            self.assertEqual(rows[-1]["status"], "suspicious")
            self.assertEqual(rows[-1]["severity"], "high")
            self.assertEqual(
                manifest["completion_proofs"][-1]["diagnostic_refs"],
                [rows[-1]["observation_ref"]],
            )

    def test_tamper_removed_during_repair_recovers_clean(self) -> None:
        writer = ScriptedWriter(
            ([_edit("tests/test_mod.py"), _run_event(False)], RunResult("done?", "done", 3)),
            ([_edit("src/mod.py"), _run_event(True)], RunResult("fixed", "done", 2)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = Path(td) / "project"
            (project / "src").mkdir(parents=True)
            (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
            state = server.AppContext(Path(td) / "state")
            event = _run(
                _runner_with_changes(
                    state,
                    writer,
                    [_changes("tests/test_mod.py"), _changes("src/mod.py")],
                ),
                state,
                project,
                "Change src/mod.py VALUE from 1 to 2 and verify",
            )

            self.assertEqual(event["stop_reason"], "done")
            receipt = event["receipt"]
            # The final collection shows the fixture restored and the
            # production file fixed: the earlier suspicion is gone.
            self.assertEqual(receipt["verification"]["trust"], "trusted")
            self.assertEqual(
                receipt["display"]["summary"],
                "1 file changed · checks passed",
            )
            manifest = _trace_payload(state)
            self.assertEqual(manifest["completion_edit_integrity"][-1]["status"], "clean")


class MonitorFailureTests(unittest.TestCase):
    def test_monitor_error_receipt_is_limited_and_traced(self) -> None:
        from codey.completion.edit_integrity import EditIntegrityObservation

        error_observation = EditIntegrityObservation(
            schema_version=1,
            run_id="run-1",
            status="monitor_error",
            severity="none",
            reason_codes=("monitor_error",),
            findings=(),
            user_authorized_test_edit=False,
            affected_paths=(),
            verification_refs=(),
            change_refs=(),
            observation_ref="edit_integrity:0123456789abcdef",
            monitor_error_ref="sha256:0123456789abcdef",
        )
        writer = ScriptedWriter((
            [_edit("src/mod.py"), _run_event(True)],
            RunResult("implemented", "done", 2),
        ))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = Path(td) / "project"
            (project / "src").mkdir(parents=True)
            (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
            state = server.AppContext(Path(td) / "state")
            with mock.patch.object(
                completion_engine_module,
                "observe_edit_integrity",
                return_value=error_observation,
            ):
                event = _run(
                    _runner(state, writer, files=("src/mod.py",)),
                    state,
                    project,
                    "Change src/mod.py VALUE from 1 to 2 and verify",
                )

            self.assertEqual(event["stop_reason"], "done")
            receipt = event["receipt"]
            # A monitor that could not observe can never read as clean.
            self.assertEqual(receipt["verification"]["trust"], "limited")
            self.assertEqual(
                receipt["display"]["summary"],
                "1 file changed · verification limited",
            )
            manifest = _trace_payload(state)
            self.assertEqual(
                manifest["completion_edit_integrity"][0]["status"],
                "monitor_error",
            )
            facts = state.project_facts.load(str(project))
            self.assertEqual(facts.successful_changes, ())


class TerminalReceiptProjectionTests(unittest.TestCase):
    def test_terminal_event_adds_durable_receipt_even_when_in_memory_event_lacks_one(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = Path(td) / "project"
            project.mkdir()
            state = server.AppContext(Path(td) / "state")
            assert state.run_ledgers is not None
            writer = state.run_ledgers.open(
                run_id="run-terminal",
                session_id="s-integrity",
                project=str(project),
                task="Change src/mod.py VALUE from 1 to 2 and verify",
                provider="deepseek",
                mode="agent",
            )
            observation = observe_edit_integrity(
                task="Change src/mod.py VALUE from 1 to 2 and verify",
                changes=_changes("src/mod.py"),
                diff=_CLEAN_DIFF,
                files=("src/mod.py",),
                decision=None,
                run_id="run-terminal",
            )
            receipt = build_task_receipt(
                {"mode": "git", "changed_count": 1},
                integrity=observation,
                checks_passed=True,
            )
            writer.append_changes_collected(
                _changes("src/mod.py"),
                checks_passed=True,
                receipt=receipt.to_dict(),
            )
            raw_event = {
                "type": "task_done",
                "run_id": "run-terminal",
                "session_id": "s-integrity",
                "summary": "ERROR: late failure",
                "stop_reason": "error",
                "turns": 0,
                "max_turns": 1,
                "provider": "deepseek",
                "mode": "agent",
            }

            projected = event_with_projected_receipt(
                state.run_ledgers,
                raw_event,
                session_id="s-integrity",
                run_id="run-terminal",
            )

            self.assertEqual(projected["receipt"], receipt.to_dict())
            self.assertNotIn("receipt", raw_event)


if __name__ == "__main__":
    unittest.main()
