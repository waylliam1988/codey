"""Exercise the current memory drawer through its production browser runtime."""
from __future__ import annotations

from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def memory_summary(*, enabled=True):
    item = {"item_id": "memory-1", "kind": "preference", "text": "Use concise answers",
            "scope": "session:a", "source_refs": ["source-1"]}
    return {"ok": True, "enabled": enabled, "scope": {"session_id": "a", "project": ""},
            "items": [item], "pending": [{"proposal_id": "proposal-1", "version": "v1", "item": item}]}


def test_failed_accept_preserves_proposal_and_can_retry(page):
    reads = []
    actions = []
    def summary(route):
        reads.append(route.request.url)
        data = memory_summary()
        if len(actions) > 1:
            data["pending"] = []
        route.fulfill(json=data)
    def action(route):
        actions.append(route.request.post_data_json)
        route.fulfill(status=500 if len(actions) == 1 else 200,
                      json={"ok": len(actions) > 1, "error": "activation failed"})
    page.route("**/api/ghost/summary?*", summary)
    page.route("**/api/ghost/action", action)
    page.evaluate("LocalContextDrawer.open()")
    expect(page.locator("#local-context-subtitle")).to_have_text("1 memories · 1 pending")
    page.get_by_role("button", name="Confirm", exact=True).click()
    expect(page.locator("#local-context-subtitle")).to_have_text("activation failed")
    expect(page.get_by_role("button", name="Confirm", exact=True)).to_be_visible()
    assert len(reads) == 1
    page.get_by_role("button", name="Confirm", exact=True).click()
    expect(page.locator("#local-context-subtitle")).to_have_text("1 memories · 0 pending")
    assert actions == [{"session_id": "a", "project": "", "action": "accept",
                        "proposal_id": "proposal-1", "version": "v1"}] * 2
    assert len(reads) == 2
    expect(page.locator(".local-context-row")).to_have_count(1)
    expect(page.locator(".local-context-row")).to_contain_text("source-1")


def test_disabled_memory_keeps_saved_items_without_proposal_form(page):
    data = memory_summary(enabled=False)
    data["pending"] = []
    page.route("**/api/ghost/summary?*", lambda route: route.fulfill(json=data))
    page.evaluate("LocalContextDrawer.open()")
    expect(page.locator("#local-context-subtitle")).to_have_text("Memory disabled")
    expect(page.locator(".local-context-row")).to_contain_text("Use concise answers")
    expect(page.get_by_role("button", name="Propose memory", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Delete memories in this scope", exact=True)).to_have_count(0)
    page.evaluate("LocalContextDrawer.close()")
    expect(page.locator("#local-context-drawer")).not_to_have_class("open")
