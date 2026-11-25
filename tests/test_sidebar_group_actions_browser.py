"""Group header actions use the packaged sidebar and existing HTTP entry points."""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from codey.app import folder_picker, server
from codey.app.context import AppContext
from codey.app.public_event_feed import PublicEventFeed


@pytest.fixture
def group_app_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    ctx = AppContext(tmp_path / "state", start_scheduler=False)
    ctx.durable.create_conversation("original")
    ctx.save_ui_state({
        "active_id": "original",
        "projects": [{"id": "project", "name": "Project Alpha", "path": str(tmp_path), "expanded": True}],
        "sessions": [{"id": "original", "title": "Original chat", "projectId": "project"}],
    }, base_revision=0)
    monkeypatch.setattr(server.CodeyHTTPServer, "app_context", ctx)
    httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    feed = PublicEventFeed()
    httpd.public_feed = feed
    detach = ctx.event_bus.observe(feed.publish)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        yield httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/")
    finally:
        detach()
        feed.close()
        httpd.shutdown()
        httpd.server_close()
        worker.join(5)
        ctx.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_group_actions_hover_keyboard_scope_and_picker(
    group_app_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool,
) -> None:
    calls: list[tuple[str, str | None]] = []
    chosen = tmp_path / "Added Project"
    chosen.mkdir()

    def pick_folder(mode: str, initial: str) -> dict[str, object]:
        calls.append((mode, initial))
        return folder_picker.folder_selection(None if cancel else str(chosen))

    monkeypatch.setattr(folder_picker, "_run_dialog", pick_folder)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(group_app_url)
        expect(page.locator(".project-main")).to_have_text("Project Alpha")
        headers = page.locator(".group-label")
        projects = headers.filter(has_text="PROJECTS")
        chats = headers.filter(has_text="CHATS")
        add_project = projects.get_by_role("button", name="Add project", exact=True)
        new_chat = chats.get_by_role("button", name="New chat", exact=True)
        page.locator("#task").click()
        page.locator("#task").hover()
        for header, action in ((projects, add_project), (chats, new_chat)):
            expect(action).to_have_css("opacity", "0")
            before = header.bounding_box()
            header.hover()
            expect(action).to_have_css("opacity", "1")
            action.hover()
            expect(action).to_have_css("background-color", "rgba(0, 0, 0, 0)")
            expect(action).to_have_css("color", "rgb(230, 230, 230)")
            assert header.bounding_box() == before
            page.locator("#task").hover()
            action.focus()
            page.keyboard.press("Tab")
            page.keyboard.press("Shift+Tab")
            expect(action).to_be_focused()
            expect(action).to_have_css("opacity", "1")
            expect(action).to_have_css("outline-style", "solid")
            page.locator("#task").click()
        assert "new-chat.svg" in new_chat.locator(".new-chat-icon").evaluate("el => getComputedStyle(el).maskImage")
        expect(add_project.locator("svg")).to_have_attribute("viewBox", "0 0 24 24")
        projects.hover()
        with (page.expect_response("**/api/pick_folder?*") as picked,
              page.expect_response("**/api/pick_folder") as started):
            add_project.click()
        assert started.value.status == 202
        response = picked.value
        receipt = {"request": started.value.request.post_data, "status": response.status, "response": response.text()}
        (tmp_path / "picker-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        assert response.status == 200
        assert calls == [("open", str(tmp_path))]
        expect(page.locator(".project-main")).to_have_count(1 if cancel else 2)
        chats.hover()
        new_chat.click()
        expect(page.locator("#session-list > .session-item.active")).to_have_count(1)
        expect(page.locator(".project-children .session-item")).to_have_count(1 if cancel else 2)
        page.locator(".project-row").filter(has_text="Project Alpha").hover()
        page.get_by_role("button", name="New chat in Project Alpha", exact=True).click()
        expect(page.locator(".project-children .session-item.active")).to_have_count(1)
        expect(page.locator("#session-list > .session-item.active")).to_have_count(0)
        page.locator("#chat-search-toggle").click()
        search = page.locator("#chat-search")
        search.fill("Project Alpha")
        expect(page.locator(".group-label:visible")).to_have_text("PROJECTS")
        expect(new_chat).to_be_hidden()
        search.fill("no-matching-project-or-chat")
        expect(page.locator(".group-label:visible")).to_have_count(0)
        assert not errors
        browser.close()


def test_group_actions_are_visible_with_touch(group_app_url: str) -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
        page.goto(group_app_url)
        expect(page.locator(".project-main")).to_have_text("Project Alpha")
        actions = page.locator(".group-action")
        expect(actions).to_have_count(2)
        for action in actions.all():
            expect(action).to_have_css("opacity", "1")
            expect(action).to_have_css("pointer-events", "auto")
            expect(action).to_have_css("width", "44px")
            expect(action).to_have_css("height", "44px")
        browser.close()
