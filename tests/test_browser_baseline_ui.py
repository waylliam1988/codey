"""Published composer submits browser-capable tasks without a Browser toggle."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, Route, expect

from codey.policies.capabilities import BROWSER_CAPABILITIES
from codey.policies.task_policy import build_task_policy
from codey.task.entry_auth import derive_entry_auth
from codey.task.model import TaskSubmission
from codey.types.json_value import JsonObject, parse_json_object
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("research", [False, True])
def test_composer_has_no_browser_toggle_and_submits_with_default_capabilities(
    page: Page, project: bool, research: bool,
) -> None:
    captured: list[JsonObject] = []

    def submit(route: Route) -> None:
        captured.append(parse_json_object(route.request.post_data or "{}"))
        response: JsonObject = {"ok": True, "run_id": "run_" + "a" * 32, "session_id": "a"}
        route.fulfill(json=response)

    page.route("**/api/run", submit)
    if project:
        _setup_result: object = page.evaluate("""() => {
            projects.push({id:'project', name:'Project', path:'E:/project', expanded:true});
            activeSession().projectId='project'; renderChat();
        }""")
    expect(page.locator("#ctx-browser")).to_have_count(0)
    if research:
        page.locator("#ctx-research").click()
        expect(page.locator("#ctx-research")).to_have_attribute("aria-pressed", "true")
    page.locator("#task").fill("Open the website and read its search results.")
    page.locator("#send").click()
    page.wait_for_function("() => !document.querySelector('#task').value")
    assert len(captured) == 1
    body = captured[0]
    assert "requested_capabilities" not in body
    expected_kind = "hybrid" if research and project else "research" if research else "auto"
    assert body["intent"] == expected_kind
    auth = derive_entry_auth(body, project="E:/project" if project else None)
    request = TaskSubmission("a", "E:/project" if project else None, "Inspect", 8, False, "local",
                             requested_capabilities=auth.requested_capabilities,
                             strict_research=auth.strict_research)
    policy = build_task_policy(request, task_kind=expected_kind)
    assert all(policy.allows(grant) for grant in BROWSER_CAPABILITIES)
