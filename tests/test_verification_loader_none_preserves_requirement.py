"""Loader returning None must preserve the requirement and block.

None is a load failure, not a legal empty set. Only () is a legal empty.
"""
from __future__ import annotations

from codey.completion.verification_policy import VerificationCandidate
from codey.operations.completion_gate import evaluate
from codey.operations.project_verification import refresh_verification_candidates
from codey.operations.task_loop import KernelExecutionDeps, KernelObservationDeps, KernelRunRequest, KernelTransportDeps
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.observe.execution_evidence import ExecutionEvidence

FP = "sha256:" + "b" * 64


def make_session():
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project="",
        max_turns=3,
    )
    session.edited_files = {"a.py": 1}
    session.set_workspace_state(1, FP)
    pytest_req = VerificationCandidate("python -m pytest", ".", "project")
    session.verification_candidates = (pytest_req,)
    session.verification_candidates_epoch = -1
    from codey.completion.verification_policy import select_verification_candidate

    session.selected_verification = select_verification_candidate(
        session.verification_candidates, tuple(session.edited_files)
    )
    assert session.selected_verification is not None
    return session


def _context(session):
    return {
        "execution_evidence": ExecutionEvidence(
            workspace_revision=session.workspace_revision,
            workspace_fingerprint=session.workspace_fingerprint,
        ),
        "project": "",
        "scope_files": ("a.py",),
        "run_id": "loader-none",
        "task": "fix a.py",
    }


def test_loader_none_preserves_requirement_and_blocks():
    session = make_session()
    old_epoch = session.verification_candidates_epoch
    session.verification_candidate_loader = lambda: None

    refresh_verification_candidates(session)

    assert session.verification_candidates_refresh_failed is True
    assert session.verification_candidates_epoch == old_epoch
    assert session.selected_verification is not None
    assert session.selected_verification.command == "python -m pytest"
    # compileall success must not substitute the required pytest.
    session.record_verification(
        "python -m compileall .", 1, True, exit_code=0,
        workspace_revision=1, workspace_fingerprint=FP, cwd=".",
    )
    assert evaluate(session, "done").complete is False
    assert evaluate(session, "done", context=_context(session)).complete is False


def test_loader_none_then_legal_recovery_allows_completion():
    session = make_session()
    session.verification_candidate_loader = lambda: None
    refresh_verification_candidates(session)
    assert session.verification_candidates_refresh_failed is True

    req = VerificationCandidate("python -m pytest", ".", "project")
    session.verification_candidate_loader = lambda: (req,)
    refresh_verification_candidates(session)
    assert session.verification_candidates_refresh_failed is False
    session.record_verification(
        "python -m pytest", 1, True, exit_code=0,
        workspace_revision=1, workspace_fingerprint=FP, cwd=".",
    )
    assert evaluate(session, "done").complete is True


def test_loader_none_blocks_real_kernel_compileall(tmp_path, monkeypatch):
    """Real kernel: pytest required, loader None, compileall pass, done -> blocked."""
    import json

    from codey.operations.task_loop import run_task_kernel
    from codey.workspace.revision import workspace_fingerprint

    monkeypatch.setenv("NATIVE_TOOLS", "0")
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    real_fp = workspace_fingerprint(str(tmp_path))
    assert real_fp, "tmp project must produce a real fingerprint"
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project=str(tmp_path),
        max_turns=3,
    )
    session.record_edit("a.py", revision=1)
    session.set_workspace_state(1, real_fp)
    pytest_req = VerificationCandidate("python -m pytest", ".", "project")
    session.verification_candidates = (pytest_req,)
    session.verification_candidates_epoch = -1
    from codey.completion.verification_policy import select_verification_candidate

    session.selected_verification = select_verification_candidate(
        session.verification_candidates, tuple(session.edited_files)
    )
    session.verification_candidate_loader = lambda: None

    class WebDone:
        def new_chat(self):
            pass

        def send(self, text):
            return json.dumps({"tool": "done", "args": {"summary": "finished"}})

        def close(self):
            pass

    # Simulate the model running compileall successfully before proposing done:
    # record the substitute verification directly, then let the kernel evaluate done.
    session.record_verification(
        "python -m compileall .", 1, True, exit_code=0,
        workspace_revision=1, workspace_fingerprint=real_fp, cwd=".",
    )
    result = run_task_kernel(
        session,
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=WebDone(),
                provider_id="web",
                run_id="loader-none-kernel",
                user_task="fix a.py",
            ),
            execution=KernelExecutionDeps(
                executors={},
            ),
            observation=KernelObservationDeps(
                completion_context=None,
            ),
        ),
    )
    assert result.completed is False
