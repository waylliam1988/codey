from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy


def test_user_requested_web_task_requires_opened_source_before_done() -> None:
    from codey.operations.completion_gate import evaluate

    policy = TaskPolicy(
        grants=frozenset({"project.read", "project.write", "project.verify", "web.read", "control"}),
        sources_open_required=True,
        required_checks=("research_sources_opened",),
    )
    session = TaskSession(policy=policy, task_kind="project", project="project")
    session.edited_files = {"a.py": 1}
    session.record_verification("python -m pytest", 1, True, exit_code=0)

    verdict = evaluate(session, "done")

    assert not verdict.complete
    assert "research_sources_opened" in str(getattr(verdict, "proof", "")) + verdict.followup
