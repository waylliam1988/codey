from dataclasses import dataclass

from codey.task.model import TaskSubmission
from tests.support.task_entry_helpers import submission


def test_negated_url_does_not_grant_web_read() -> None:
    from codey.app.api import derive_entry_auth

    auth = derive_entry_auth(
        {
            "intent": "auto",
            "task": "不要浏览 https://example.com，只检查本地代码", "allow_web": False,
        },
        project="/tmp/project",
    )

    assert "web.read" not in auth.requested_capabilities


def test_model_research_route_cannot_add_web_grant() -> None:
    from codey.operations.task_entry import build_task_policy_for_entry

    @dataclass(frozen=True)
    class RoutedSubmission(TaskSubmission):
        model_hint: str = "ACTION: research\nPLAN: inspect docs"

    request = submission()
    policy = build_task_policy_for_entry(request, "project")
    routed_request = RoutedSubmission(
        session_id=request.session_id, project=request.project, task=request.task,
        max_turns=request.max_turns, continue_task=request.continue_task,
        provider_id=request.provider_id,
    )
    routed = build_task_policy_for_entry(routed_request, "project")
    assert routed.grants == policy.grants

    assert not routed.allows("web.read")
