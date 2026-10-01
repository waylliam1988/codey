"""Kernel and soak completion views come from real runs, not hand-filled dicts.

Locks: a deterministic fake provider driving run_task_kernel through real
tool execution produces a completed run; its gate view passes the oracle;
SoakContext.completion_views carries real views and assert_valid checks each
one (no zero-iteration wiring).
"""
from __future__ import annotations


class _DoneThenCloseProvider:
    name = "fake"

    def __init__(self):
        self.prompts: list[str] = []
        self.closed = False

    def new_chat(self):
        return None

    def send(self, prompt: str, timeout: float | None = None) -> str:
        self.prompts.append(prompt or "")
        return '{"tool": "done", "args": {"summary": "read-only summary"}}'

    def close(self):
        self.closed = True


def _readonly_session(tmp_path):
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    return TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read"})),
        task_kind="project",
        project=str(tmp_path),
        max_turns=2,
        task_text="Summarize a.txt. Do not modify files.",
    )


def test_kernel_readonly_run_completes_with_real_tool_loop(tmp_path):
    from codey.operations.task_loop import run_task_kernel

    session = _readonly_session(tmp_path)
    provider = _DoneThenCloseProvider()
    result = run_task_kernel(
        session, provider=provider, run_id="r-kernel-view",
        project_path=tmp_path, provider_id="fake",
        user_task="Summarize a.txt. Do not modify files.",
    )
    assert result.completed is True
    assert result.stop_reason == "done"
    assert provider.prompts, "kernel must actually send to the provider"


def test_gate_view_from_kernel_session_passes_oracle(tmp_path):
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_loop import run_task_kernel
    from codey.runtime.observe.execution_evidence import ExecutionEvidence
    from tests.stress.oracle import InvariantChecker, completion_view_from_gate

    session = _readonly_session(tmp_path)
    provider = _DoneThenCloseProvider()
    result = run_task_kernel(
        session, provider=provider, run_id="r-kernel-oracle",
        project_path=tmp_path, provider_id="fake",
        user_task="Summarize a.txt. Do not modify files.",
    )
    assert result.completed is True
    evidence = ExecutionEvidence()
    verdict = evaluate(session, result.summary or "done", context=None)
    assert verdict.complete is True
    view = completion_view_from_gate(session=session, evidence=evidence, verdict=verdict)
    InvariantChecker().check_completion_truthful(view)


def test_soak_context_carries_real_completion_views(tmp_path):
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.observe.execution_evidence import ExecutionEvidence
    from codey.workspace.revision import workspace_fingerprint
    from tests.stress.oracle import InvariantChecker
    from tests.stress.scheduler import SoakContext

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    fp = workspace_fingerprint(str(tmp_path))
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project=str(tmp_path),
    )
    session.record_edit("a.py", revision=1)
    session.set_workspace_state(7, fp)
    session.record_verification(
        "python -m pytest", 1, True, exit_code=0,
        workspace_revision=7, workspace_fingerprint=fp, cwd=".",
    )
    evidence = ExecutionEvidence(workspace_revision=7, workspace_fingerprint=fp)
    verdict = evaluate(session, "done", context={
        "execution_evidence": evidence,
        "project": str(tmp_path),
        "scope_files": ("a.py",),
        "run_id": "soak-completion",
        "task": "fix",
    })
    assert verdict.complete is True
    ctx = SoakContext(tmp_path)
    try:
        ctx.record_real_completion_view(session, evidence, verdict)
        assert len(ctx.completion_views) == 1
        calls: list[dict] = []
        checker = InvariantChecker()
        original = checker.check_completion_truthful

        def _track(v: dict) -> None:
            calls.append(v)
            return original(v)

        checker.check_completion_truthful = _track  # type: ignore[method-assign]
        checker.assert_valid(
            {"log_rows": [], "ghost_rows": []},
            unknowns=[],
            completion_views=ctx.completion_views,
        )
        assert len(calls) == len(ctx.completion_views) == 1
    finally:
        ctx.close()
