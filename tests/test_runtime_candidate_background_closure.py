"""The fixed validator receives formal background results without repeating execution."""
from pathlib import Path

import pytest

from codey.completion.verification_policy import VerificationCandidate
from codey.operations.completion_context import CompletionContext
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
)
from codey.operations.project_candidate_validation import _AdmittedCandidateCheck
from codey.operations.provider_session import adapt_provider
from codey.operations.task_loop import run_task_kernel
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from tests.test_tool_task_background import open_runtime


@pytest.mark.parametrize("passing", [True, False])
@pytest.mark.parametrize("budget", [2, 8])
def test_fixed_validator_consumes_terminal_receipt_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                      passing: bool, budget: int) -> None:
    # Force the real durable path to yield, without making the test wait ten seconds.
    monkeypatch.setattr("codey.operations.tool_tasks.COMMAND_YIELD_MS", 0)
    runtime = open_runtime(tmp_path)
    try:
        runtime.session.max_turns = budget
        release = tmp_path / "release"
        (runtime.project / "test_check.py").write_text(
            "import time\nfrom pathlib import Path\ndef test_check():\n"
            f"    while not Path({str(release)!r}).exists():\n        time.sleep(0.01)\n"
            f"    assert {passing!r}\n", encoding="utf-8")
        command = "python -m pytest -q"
        runtime.session.policy = TaskPolicy(runtime.session.policy.grants,
            required_checks=(require_tool_success("run", arguments={"command": command}),))
        runtime.app.durable_scheduler.start()
        class ReleasingCheck(_AdmittedCandidateCheck):
            sends: int = 0

            def send(self, text: str, timeout: float | None = None) -> str:
                self.sends += 1
                if self.sends == 2:
                    release.write_text("ready", encoding="utf-8")
                return super().send(text, timeout)

        provider = ReleasingCheck((VerificationCandidate(command, "."),))
        evidence = ExecutionEvidence()
        result = run_task_kernel(runtime.session, request=KernelRunRequest(
            KernelTransportDeps(adapt_provider(provider), run_id="r", provider_id="test"),
            KernelExecutionDeps(intent_sink=runtime.sink, project_path=runtime.project,
                                workspace_revision_store=runtime.app.workspace_revisions),
            KernelObservationDeps(completion_context=CompletionContext(execution_evidence=evidence),
                on_boundary=durable_generation_boundary_callback(
                durable=runtime.app.durable, session_id="s", task_id="r", sink=runtime.sink))))
        assert result.completed is (passing and budget > 2), result
        assert result.stop_reason == ("max_turns" if budget == 2 else "done" if passing else "no_progress")
        assert result.turns <= budget
        if passing and budget > 2:
            assert result.summary == "Submit the checked candidate for validation."
            assert provider.sends == 3
        children = runtime.app.durable.owned_tasks("r")
        assert len(children) == 1
        assert len(runtime.app.durable.effects(children[0].task_id)) == 1
        assert len(runtime.session.verifications) == 1
        assert runtime.session.verifications[0]["passed"] is passing
        # Multiple restores must leave one formal observation in the parent's
        # review/receipt projection, even without a parent tool-finished event.
        checks = [*evidence.checks_after_edit, *evidence.failed_checks_after_edit]
        assert len(checks) == 1
        assert checks[0].exit_code == (0 if passing else 1)
    finally:
        assert runtime.app.close()
