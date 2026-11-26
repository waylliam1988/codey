"""Exercise the current memory drawer through its production browser runtime."""
from __future__ import annotations

from uuid import UUID

from playwright.sync_api import Page, Route, expect

from codey.types.json_value import JsonObject, parse_json_object
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def memory_summary(*, enabled: bool = True) -> JsonObject:
    item: JsonObject = {"item_id": "memory-1", "kind": "preference", "text": "Use concise answers",
            "scope": "session:a", "source_refs": ["source-1"], "slot": "", "context": "", "expires_at": ""}
    return {"ok": True, "enabled": enabled, "scope": {"session_id": "a", "project": ""},
            "controls": {"accept_events": True, "retrieve_projections": True,
                         "run_maintenance": True, "admit_hints": True},
            "records": [], "exposures": [], "items": [item], "pending": [{"proposal_id": "proposal-1", "version": "v1", "item": item}]}


def test_failed_accept_preserves_proposal_and_can_retry(page: Page) -> None:
    reads: list[str] = []
    actions: list[JsonObject] = []
    def summary(route: Route) -> None:
        reads.append(route.request.url)
        data = memory_summary()
        if len(actions) > 1:
            data["pending"] = []
        route.fulfill(json=data)
    def action(route: Route) -> None:
        value = parse_json_object(route.request.post_data or '{}')
        actions.append(value)
        route.fulfill(status=500 if len(actions) == 1 else 200,
                      json={"ok": len(actions) > 1, "error": "activation failed"})
    page.route("**/api/ghost/summary?*", summary)
    page.route("**/api/ghost/action", action)
    page.evaluate("LocalContextDrawer.open()")
    expect(page.locator("#local-context-subtitle")).to_have_text("1 items · 1 pending")
    drawer = page.locator("#local-context-drawer")
    for label in ("Confirm", "Reject", "Submit for review", "Clear local context…"):
        control = drawer.get_by_role("button", name=label, exact=True)
        expect(control).to_have_css("background-color", "rgba(0, 0, 0, 0)")
        expect(control).to_have_css("border-top-width", "0px")
    expect(drawer.get_by_role("button", name="Submit for review")).to_be_disabled()
    drawer.get_by_role("button", name="Clear local context…", exact=True).click()
    expect(drawer.get_by_role("button", name="Cancel", exact=True)).to_be_focused()
    page.keyboard.press("Escape")
    expect(drawer.get_by_role("button", name="Clear local context…", exact=True)).to_be_focused()
    expect(drawer.get_by_role("button", name="Clear local context", exact=True)).to_be_hidden()
    assert actions == []
    page.get_by_role("button", name="Confirm", exact=True).click()
    expect(page.locator("#local-context-subtitle")).to_have_text("activation failed")
    expect(page.get_by_role("button", name="Confirm", exact=True)).to_be_visible()
    assert len(reads) == 1
    page.get_by_role("button", name="Confirm", exact=True).click()
    expect(page.locator("#local-context-subtitle")).to_have_text("1 items · 0 pending")
    request_id = actions[0]["request_id"]
    assert isinstance(request_id, str) and str(UUID(request_id)) == request_id
    assert actions == [{"request_id": request_id, "session_id": "a", "project": "", "action": "accept",
                        "proposal_id": "proposal-1", "version": "v1"}] * 2
    assert len(reads) == 2
    expect(page.locator(".local-context-row")).to_have_count(1)
    expect(page.locator(".local-context-row")).to_contain_text("source-1")


def test_disabled_memory_keeps_saved_items_without_proposal_form(page: Page) -> None:
    data = memory_summary(enabled=False)
    data["pending"] = []
    def respond(route: Route) -> None:
        route.fulfill(json=data)
    page.route("**/api/ghost/summary?*", respond)
    page.evaluate("LocalContextDrawer.open()")
    expect(page.locator("#local-context-subtitle")).to_have_text("Local context disabled")
    expect(page.locator(".local-context-row")).to_contain_text("Use concise answers")
    expect(page.get_by_role("button", name="Submit for review", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Clear local context…", exact=True)).to_have_count(0)
    page.evaluate("LocalContextDrawer.close()")
    expect(page.locator("#local-context-drawer")).not_to_have_class("open")
