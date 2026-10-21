"""Real packaged UI and Codey HTTP endpoints; no injected messages or event handlers."""
from __future__ import annotations

import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from codey.app import server
from codey.app.context import AppContext


def test_workspace_files_history_search_branches_and_layout_restore(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'long.py').write_text('\n'.join(f'line_{index} = {index}' for index in range(1, 301)), encoding='utf8')
    ctx = AppContext(tmp_path / 'state', start_scheduler=False)
    ctx.durable.create_conversation('chat')
    target = ''
    for index in range(65):
        user = ctx.durable.append_message('chat', 'user_message', {'text': f'Question text {index} · unique needle {index}'})
        if index == 3:
            target = user.entry_id
        ctx.durable.append_message('chat', 'assistant_message', {'text': f'Answer {index}\n\n' + 'Readable text. ' * 12})
    ctx.durable.append_message('chat', 'assistant_message', {'text': 'Open `long.py:180` to inspect the complete source.'})
    ctx.save_ui_state({'active_id': 'chat', 'projects': [{'id': 'p', 'name': 'Codey', 'path': str(project), 'expanded': True}], 'sessions': [{'id': 'chat', 'title': 'Workspace test', 'projectId': 'p'}, {'id': 'other', 'title': 'Other chat', 'projectId': 'p'}]}, base_revision=0)
    monkeypatch.setattr(server.CodeyHTTPServer, "app_context", ctx)
    httpd = server.CodeyHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    output = Path('.e2e-artifacts/uiux-implementation')
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 900})
            page.set_default_timeout(15000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(httpd.launch_url(f'http://127.0.0.1:{httpd.server_port}/'))
            expect(page.locator('.msg.asst').last).to_contain_text('complete source')
            page.get_by_role('button', name='long.py:180', exact=True).click()
            pane = page.locator('.document-pane')
            expect(pane.locator('[data-line="300"]')).to_contain_text('line_300')
            expect(pane.locator('.selected-line')).to_contain_text('line_180')
            assert pane.locator('.document-body').evaluate('el => el.scrollTop > 0')
            pane.get_by_role('searchbox', name='Find in document').fill('line_299')
            pane.get_by_role('button', name='Next match', exact=True).click()
            expect(pane.locator('mark.current-match')).to_have_text('line_299')
            reading_top = pane.locator('.document-body').evaluate('el => el.scrollTop')
            page.screenshot(path=str(output / 'files-1440.png'))
            page.get_by_role('button', name='Close workspace', exact=True).click()
            expect(page.locator('.workspace')).to_be_hidden()
            assert page.locator('main').bounding_box()['width'] > 1000
            page.get_by_role('button', name='More', exact=True).last.click()
            page.locator('#top-menu').get_by_role('menuitem', name='Search this conversation', exact=True).click()
            page.get_by_role('searchbox', name='Search conversation history').fill('unique needle 3')
            page.locator('.conversation-find').get_by_role('button', name='Search', exact=True).click()
            page.locator('[data-search-hit="0"]').click()
            expect(page.locator(f'[data-entry-id="{target}"]')).to_contain_text('Question text 3')
            assert page.locator('.msg.user').count() < 65
            page.get_by_role('button', name='Close conversation search', exact=True).click()
            expect(page.get_by_role('button', name='long.py:180', exact=True)).to_be_visible()
            page.get_by_role('button', name='Open workspace', exact=True).click()
            page.get_by_role('menuitem', name='Files', exact=True).click()
            expect(pane).to_be_visible()
            page.reload()
            expect(pane.locator('[data-line="300"]')).to_contain_text('line_300')
            expect(pane.locator('.document-body')).to_have_js_property('scrollTop', reading_top)
            expect(page.get_by_role('tab', name='Files', exact=True)).to_have_attribute('aria-selected', 'true')
            for width, height in [(1280, 800), (1920, 1080), (970, 900), (600, 800)]:
                page.set_viewport_size({'width': width, 'height': height})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                expect(pane.locator('[data-line="300"]')).to_be_attached()
                page.screenshot(path=str(output / f'files-{width}.png'))
            page.set_viewport_size({'width': 1440, 'height': 900})
            page.get_by_role('button', name='Close workspace', exact=True).click()
            page.get_by_role('button', name='Open workspace', exact=True).click()
            page.get_by_role('menuitem', name='Branches and workflow', exact=True).click()
            expect(page.locator('.workspace')).to_contain_text('Conversation branches')
            page.locator('.conversation-branch').get_by_role('button', name='Fork conversation', exact=True).last.click()
            expect(page.locator('.sess-title')).to_contain_text('Fork')
            expect(page.locator('#session-list')).to_contain_text('Fork')
            page.keyboard.press('Control+k')
            palette = page.get_by_role('dialog', name='Commands', exact=True)
            expect(palette).to_be_visible()
            palette.get_by_role('combobox', name='Find an action', exact=True).fill('search this conversation')
            page.keyboard.press('Enter')
            expect(page.locator('.conversation-find')).to_be_visible()
            page.keyboard.press('Escape')
            page.reload()
            expect(page.locator('#session-list')).to_contain_text('Fork')
            for width, height in [(1280, 800), (1920, 1080), (970, 900), (600, 800)]:
                page.set_viewport_size({'width': width, 'height': height})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors
            page.set_viewport_size({'width': 1440, 'height': 900})
            page.emulate_media(reduced_motion='reduce')
            if page.locator('#show-sidebar').is_visible():
                page.locator('#show-sidebar').click()
            page.locator('#btn-new-chat').click()
            expect(page.locator('#chat')).to_be_empty()
            expect(page.locator('main')).to_have_class('empty-conversation')
            border = page.locator('.composer-box').evaluate('el => getComputedStyle(el).borderColor')
            page.locator('#task').click()
            assert page.locator('#task').evaluate('el => getComputedStyle(el).outlineStyle') == 'none'
            assert page.locator('.composer-box').evaluate('el => getComputedStyle(el).borderColor') == border
            page.screenshot(path=str(output / 'home-1440.png'))
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
        ctx.close()
