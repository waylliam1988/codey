"""Project navigation and quiet copy controls in the packaged UI."""

import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from codey.app import server
from codey.app.context import AppContext
from tests.test_ui_inplace_render import _goto_restored_ui


def test_project_actions_preview_search_and_quiet_copy(tmp_path, monkeypatch):
    ctx = AppContext(tmp_path / "state")
    for index in range(8):
        session_id = f"chat-{index}"
        ctx.durable.create_conversation(session_id)
        ctx.durable.append_message(session_id, "assistant_message", {"text": "Saved answer"})
    ctx.save_ui_state(
        {
            "active_id": "chat-7",
            "projects": [{"id": "project", "name": "Project", "path": str(tmp_path), "expanded": True}],
            "sessions": [
                {
                    "id": f"chat-{i}",
                    "title": f"Chat {i}",
                    "projectId": "project",
                    "provider": "deepseek",
                    "messages": [],
                }
                for i in range(8)
            ],
        },
        base_revision=0,
    )
    monkeypatch.setattr(server.CodeyHTTPServer, "app_context", ctx)
    httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            _goto_restored_ui(page, httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/"))
            page.locator("#topbar-more").click()
            expect(page.locator("#top-menu [data-act=settings]")).to_have_count(0)
            page.keyboard.press("Escape")
            expect(page.locator("#btn-settings")).to_be_visible()
            children = page.locator(".project-children")
            expect(children.locator(".session-item")).to_have_count(5)
            expect(children.locator(".session-item.active")).to_contain_text("Chat 7")
            for selector, parent in (
                (".session-item.active .session-more", ".session-item.active"),
                ("#topbar-more", None),
                ("#toggle-sidebar", None),
            ):
                page.locator("#task").click()
                if parent:
                    page.locator(parent).hover()
                action = page.locator(selector)
                action.hover()
                assert action.evaluate("el => getComputedStyle(el).backgroundColor") == "rgba(0, 0, 0, 0)", selector
                assert action.evaluate("el => getComputedStyle(el).outlineStyle") == "none", selector
            page.locator("#btn-settings").click()
            page.locator("#settings-dismiss").hover()
            assert (
                page.locator("#settings-dismiss").evaluate("el => getComputedStyle(el).backgroundColor")
                == "rgba(0, 0, 0, 0)"
            )
            page.locator("#settings-dismiss").click()
            page.locator("#task").fill("draft")
            page.locator("#send").hover()
            assert page.locator("#send").evaluate("el => getComputedStyle(el).backgroundColor") == "rgba(0, 0, 0, 0)"
            page.locator("#task").fill("")
            children.get_by_role("button", name="Show more", exact=True).click()
            expect(children.locator(".session-item")).to_have_count(8)
            children.get_by_role("button", name="Show less", exact=True).click()
            expect(children.locator(".session-item")).to_have_count(5)
            page.locator("#chat-search-toggle").click()
            page.locator("#chat-search").fill("Chat 6")
            expect(children.locator(".session-item")).to_have_count(1)
            expect(children).to_contain_text("Chat 6")
            page.locator("#chat-search").fill("")
            page.locator("#task").hover()
            more = page.locator(".project-more")
            create = page.get_by_role("button", name="New chat in Project", exact=True)
            expect(create).to_have_count(1)
            assert "new-chat.svg" in create.locator(".new-chat-icon").evaluate("el => getComputedStyle(el).maskImage")
            icon = page.request.get(page.url + "assets/new-chat.svg")
            assert icon.ok
            assert 'viewBox="0 0 16 16"' in icon.text()
            for name in ("chevron-down", "arrow-down", "spinner"):
                asset = page.request.get(page.url + f"assets/{name}.svg")
                assert asset.ok
                assert "image/svg+xml" in asset.headers["content-type"]
                assert "<svg" in asset.text()
            assert create.evaluate("el => getComputedStyle(el).opacity") == "0"
            page.locator(".project-row").hover()
            expect(more).to_be_visible()
            assert create.evaluate("el => getComputedStyle(el).opacity") == "1"
            for action in (more, create):
                action.hover()
                assert action.evaluate("el => getComputedStyle(el).backgroundColor") == "rgba(0, 0, 0, 0)"
                assert action.evaluate("el => getComputedStyle(el).outlineStyle") == "none"
                assert action.evaluate("el => getComputedStyle(el).color") == page.locator(".project-row").evaluate(
                    "el => getComputedStyle(el).color"
                )
            more.click()
            expect(page.locator("#proj-menu")).to_be_visible()
            page.keyboard.press("Escape")
            page.locator("#task").hover()
            copy = page.locator(".msg-copy").first
            assert copy.evaluate("el => getComputedStyle(el).opacity") == "0"
            page.locator(".msg").first.hover()
            page.wait_for_function("getComputedStyle(document.querySelector('.msg-copy')).opacity === '1'")
            copy.hover()
            assert copy.evaluate("el => getComputedStyle(el).backgroundColor") == "rgba(0, 0, 0, 0)"
            page.locator("#task").hover()
            page.wait_for_function("getComputedStyle(document.querySelector('.msg-copy')).opacity === '0'")
            copy.focus()
            page.wait_for_function("getComputedStyle(document.querySelector('.msg-copy')).opacity === '1'")
            page.locator(".project-row").hover()
            create.click()
            expect(children.locator(".session-item.active")).to_contain_text("New chat")
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(5)
        ctx.close()


def test_local_context_more_uses_transparent_pointer_hover():
    assets = Path(__file__).resolve().parents[1] / "codey" / "web" / "assets"
    css = (assets / "tokens.css").read_text(encoding="utf-8") + (assets / "app.css").read_text(encoding="utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(
            f'<style>{css}</style><div class="local-context-row"><button class="local-context-more" aria-label="More">⋯</button></div>'
        )
        action = page.get_by_role("button", name="More", exact=True)
        resting_color = action.evaluate("el => getComputedStyle(el).color")
        action.hover()
        assert action.evaluate("el => getComputedStyle(el).backgroundColor") == "rgba(0, 0, 0, 0)"
        assert action.evaluate("el => getComputedStyle(el).color") != resting_color
        browser.close()


def test_progress_spinner_keeps_essential_motion_with_reduced_motion_enabled():
    assets = Path(__file__).resolve().parents[1] / "codey" / "web" / "assets"
    css = (assets / "tokens.css").read_text(encoding="utf-8") + (assets / "app.css").read_text(encoding="utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(reduced_motion="reduce")
        page.set_content(f'<style>{css}</style><div><span class="spinner"></span> Running</div>')
        spinner = page.locator(".spinner")
        assert spinner.evaluate("el => getComputedStyle(el).animationName") == "spin"
        assert spinner.evaluate("el => getComputedStyle(el).display") == "inline-block"
        before = spinner.evaluate("el => getComputedStyle(el).transform")
        page.wait_for_function("getComputedStyle(document.querySelector('.spinner')).transform !== " + repr(before))
        browser.close()


def test_process_and_thinking_use_the_command_disclosure_language():
    assets = Path(__file__).resolve().parents[1] / "codey" / "web" / "assets"
    css = (assets / "tokens.css").read_text(encoding="utf-8") + (assets / "app.css").read_text(encoding="utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(
            f'<style>{css}</style><details class="process-group"><summary class="process-summary">Worked</summary></details><details class="thinking"><summary>Thinking</summary></details><div class="command-card"><details><summary>Ran</summary></details></div>'
        )
        command_font = page.locator(".command-card summary").evaluate("el => getComputedStyle(el).font")
        for selector in (".process-summary", ".thinking > summary"):
            summary = page.locator(selector)
            assert summary.evaluate("el => getComputedStyle(el).listStyleType") == "none"
            assert summary.evaluate("el => getComputedStyle(el).font") == command_font
            assert "chevron-down.svg" in summary.evaluate("el => getComputedStyle(el, '::after').maskImage")
        browser.close()


def test_approval_command_and_composer_share_one_radius_and_quiet_title():
    assets = Path(__file__).resolve().parents[1] / "codey" / "web" / "assets"
    css = (assets / "tokens.css").read_text(encoding="utf-8") + (assets / "app.css").read_text(encoding="utf-8")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(f'<style>{css}</style><div class="shell-card"><div class="sc-title">Run this command?</div></div><div class="command-panel">Shell</div><div class="composer-box">Input</div>')
        for selector in (".shell-card", ".command-panel", ".composer-box"):
            assert page.locator(selector).evaluate("el => getComputedStyle(el).borderRadius") == "10px"
        assert page.locator(".sc-title").evaluate("el => getComputedStyle(el).fontSize") == "13px"
        browser.close()
