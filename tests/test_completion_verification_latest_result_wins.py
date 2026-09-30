from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy

_VALID_FP = "sha256:" + "a" * 64


def test_same_revision_failed_then_successful_verification_completes() -> None:
    from codey.operations.completion_gate import evaluate

    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"project.verify", "control"})),
        project="project",
        task_kind="project",
        project_changes_required=False,
    )
    session.edited_files = {"a.py": 1}
    session.set_workspace_state(1, _VALID_FP)
    session.record_verification("python -m pytest", 1, False, exit_code=1,
                                workspace_revision=1, workspace_fingerprint=_VALID_FP)
    session.record_verification("python -m pytest", 1, True, exit_code=0,
                                workspace_revision=1, workspace_fingerprint=_VALID_FP)

    verdict = evaluate(session, "done")

    assert verdict.complete
