from types import SimpleNamespace


def test_negated_url_does_not_grant_web_read() -> None:
    from codey.app.api import derive_entry_auth

    auth = derive_entry_auth(
        {
            "intent": "auto",
            "task": "不要浏览 https://example.com，只检查本地代码",
        },
        project="/tmp/project",
    )

    assert "web.read" not in auth.requested_capabilities


def test_model_research_route_cannot_add_web_grant() -> None:
    from codey.operations.task_entry import build_task_policy_for_entry

    request = SimpleNamespace(
        project="/tmp/project",
        requested_capabilities=(),
        strict_research=False,
        project_changes_required=False,
    )
    policy = build_task_policy_for_entry(request, "project")
    request.model_hint = "ACTION: research\nPLAN: inspect docs"
    routed = build_task_policy_for_entry(request, "project")
    assert routed.grants == policy.grants

    assert not routed.allows("web.read")
