"""Packaged browser checks, optionally against the user's real project."""

import os
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from codey.app import server
from codey.app.context import AppContext
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from tests.test_ui_inplace_render import _goto_restored_ui


def test_shell_card_and_workspace_research_preview_share_packaged_ui(tmp_path, monkeypatch):
    project = Path(os.environ.get("UI_SMOKE_PROJECT", str(tmp_path / "project")))
    project.mkdir(exist_ok=True)
    actual = bool(os.environ.get("UI_SMOKE_PROJECT"))
    relative = "docs/天真蓝自拍照片开放平台接口测试报告.md" if actual else "report.md"
    if not actual:
        (project / relative).write_text("# Local report\n\n" + "参数正常\n\n" * 200, encoding="utf-8")
    assert (project / relative).is_file()
    ctx = AppContext(tmp_path / "state")
    ctx._knowledge_store = KnowledgeStore(tmp_path / "vault")
    ctx._knowledge_store.write_note(
        KnowledgeNote(
            id="preview-research",
            type="synthesis",
            title="Research document",
            body="# Research preview\n\nResearch body",
            session_id="preview",
        )
    )
    ctx.save_ui_state(
        {
            "active_id": "preview",
            "projects": [{"id": "p", "name": "Project", "path": str(project)}],
            "sessions": [
                {
                    "id": "preview",
                    "title": "Preview test",
                    "projectId": "p",
                    "provider": "local",
                    "messages": [
                        {"type": "asst", "text": f"产出文件：`{relative}`\n\n" + "Full answer remains visible. " * 30},
                        {
                            "type": "shell_result",
                            "command": "python -c " + "x" * 3000,
                            "output": "参数异常\n" * 1000,
                            "exitCode": 1,
                            "approved": True,
                            "shellStatus": "exit",
                            "outputTruncated": True,
                        },
                        {"type": "research_done", "text": "Research saved", "sessionId": "preview"},
                    ],
                    "researchRuns": [
                        {"runId": "research", "synthesisId": "preview-research", "notesCreated": ["preview-research"]}
                    ],
                }
            ],
        },
        base_revision=0,
    )
    monkeypatch.setattr(server, "STATE", ctx)
    httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            _goto_restored_ui(page, httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/"))
            expect(page.get_by_role("button", name="Collapse", exact=True)).to_have_count(0)
            expect(page.locator(".msg.asst .body")).to_contain_text("Full answer remains visible. " * 30)
            card = page.locator(".command-card")
            expect(card).to_be_visible()
            assert card.bounding_box()["height"] <= 32
            assert card.evaluate("el => getComputedStyle(el).borderTopWidth") == "0px"
            card.locator("summary").focus()
            page.keyboard.press("Enter")
            expect(card.locator(".command-panel-label")).to_have_text("Shell")
            assert card.locator("summary").evaluate("el => getComputedStyle(el).color") != card.locator(
                ".command-status"
            ).evaluate("el => getComputedStyle(el).color")
            output = card.locator(".shell-output")
            expect(output).to_be_visible()
            assert output.bounding_box()["height"] <= 180
            assert output.evaluate("(el)=>el.scrollHeight > el.clientHeight")
            expect(card.get_by_role("button", name="Copy retained output", exact=True)).to_be_visible()
            copy_output = card.get_by_role("button", name="Copy retained output", exact=True)
            output.hover()
            copy_output.hover()
            assert copy_output.evaluate("el => getComputedStyle(el).backgroundColor") == "rgba(0, 0, 0, 0)"
            page.get_by_role("button", name=relative, exact=True).click()
            pane = page.get_by_role("complementary", name="Document preview")
            expect(pane).to_be_visible()
            expect(pane.locator(".document-body .md-h1")).to_be_visible()
            if not actual:
                pane.locator(".document-body").evaluate("el => { el.scrollTop = 240; }")
                expect(pane.locator(".document-body")).to_have_js_property("scrollTop", 240)
                pane.get_by_role("button", name="Reload", exact=True).click()
                expect(pane.locator(".document-status")).to_have_count(0)
                expect(pane.locator(".document-body")).to_have_js_property("scrollTop", 240)
            for name in ("Reload", "Copy document", "Close preview"):
                action = pane.get_by_role("button", name=name, exact=True)
                action.hover()
                assert action.evaluate("el => getComputedStyle(el).backgroundColor") == "rgba(0, 0, 0, 0)", name
            pane.get_by_role("button", name="View source", exact=True).click()
            expect(pane.locator("pre")).to_contain_text("# ")
            pane.get_by_role("button", name="Close preview", exact=True).click()
            page.get_by_role("button", name="Open report", exact=True).click()
            expect(pane.locator(".document-body .md-h1")).to_have_text("Research preview")
            pane.get_by_role("button", name="Close preview", exact=True).click()
            page.get_by_role("button", name="Open", exact=True).click()
            page.get_by_role("button", name="Notes", exact=True).click()
            page.get_by_role("button", name="Open document", exact=True).click()
            expect(pane.locator(".document-body .md-h1")).to_have_text("Research preview")
            expect(pane.get_by_role("tab")).to_have_count(2)
            assert pane.locator(".document-tabs").bounding_box()["height"] <= 48
            page.set_viewport_size({"width": 970, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert (
                pane.locator(".document-toolbar").bounding_box()["x"]
                + pane.locator(".document-toolbar").bounding_box()["width"]
                <= 970
            )
            page.set_viewport_size({"width": 1440, "height": 1000})
            assert not errors
            if actual:
                page.wait_for_function("document.getElementById('research-drawer').getAnimations().length === 0")
                evidence = Path("E:/codey/.e2e-artifacts/document-preview")
                evidence.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(evidence / "selfie-research.png"))
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
        ctx.close()


def test_capture_truncation_flag_survives_ui_restart(tmp_path):
    from codey.storage.ui_state_store import UiStateStore

    store = UiStateStore(tmp_path)
    saved = store.save(
        {
            "active_id": "s",
            "projects": [],
            "sessions": [
                {
                    "id": "s",
                    "messages": [
                        {
                            "type": "shell_result",
                            "output": "head...tail",
                            "outputTruncated": True,
                        }
                    ],
                }
            ],
        },
        base_revision=0,
    )
    assert saved["sessions"][0]["messages"][0]["outputTruncated"] is True
