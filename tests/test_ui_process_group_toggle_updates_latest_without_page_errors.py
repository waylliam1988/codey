"""Tool/reasoning details toggles must update scrolling through a public API."""

from playwright.sync_api import expect

from tests.test_ui_workflow import page, ui_browser  # noqa: F401 -- real browser fixtures


def test_process_group_open_and_close_has_no_javascript_errors(page):  # noqa: F811
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.evaluate("""() => {
      addToSession('a', {type:'user',text:'Check the code'});
      addToSession('a', {type:'thinking',runId:'toggle-fixture',text:'Checking the code'});
    }""")
    group = page.locator(".process-group")
    expect(group).to_be_visible()
    group.locator(".process-summary").click()
    expect(group).not_to_have_attribute("open", "")
    group.locator(".process-summary").click()
    expect(group).to_have_attribute("open", "")
    page.wait_for_timeout(100)
    assert errors == []
