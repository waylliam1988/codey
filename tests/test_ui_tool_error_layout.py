"""Measure tool errors in Chromium: jsdom cannot detect overlapping text."""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from tools.frontend_source import frontend_source

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def layout_browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.mark.parametrize("assets", ["frontend/public/assets", "codey/web/assets"])
@pytest.mark.parametrize("width", [320, 800])
@pytest.mark.parametrize("compact", [False, True])
def test_tool_errors_do_not_overlap_or_overflow(layout_browser, assets, width, compact):
    page = layout_browser.new_page(viewport={"width": width, "height": 800})
    try:
        page.set_content('<div id="fixture"></div>')
        for name in ("tokens.css", "app.css"):
            page.add_style_tag(content=(ROOT / assets / name).read_text(encoding="utf-8"))
        page.add_style_tag(content="body { display:block; padding:16px; overflow:auto; }")
        page.add_script_tag(content=frontend_source("render"))
        page.evaluate("""compact => {
          const rows = [
            {kind:'search', path:'氦气 价格 上涨 2026 卡塔尔 出口 中断 半导体 液氦',
             result:'ERROR: search failed: search_timeout (duckduckgo): search time budget exhausted', error:true},
            {kind:'read_tool_result', result:'ERROR: stored result unavailable in this task. Read failure does not authorize re-execution.', error:true},
            {kind:'search', path:'helium supply shortage price October 2026 Qatar Ras Laffan restart',
             result:'ERROR: search failed: Page.route: Target page, context or browser has been closed', error:true},
            {kind:'read_tool_result', result:'ERROR: '+ 'unbroken_output_'.repeat(30), error:true},
            {kind:'search', path:'氦气价格最新信息', result:'8 results'},
            {kind:'search', path:'query in progress', pending:true, activity:'Working'}
          ];
          for (const row of rows) document.querySelector('#fixture').append(Render.toolRowEl(row, compact));
        }""", compact)
        measurements = page.locator(".tool-line").evaluate_all("""rows => rows.map(row => {
          const bounds = row.getBoundingClientRect();
          const result = row.querySelector('.tl-result').getBoundingClientRect();
          const path = row.querySelector('.tl-path').getBoundingClientRect();
          const kind = row.querySelector('.tl-kind');
          const name = kind && kind.getBoundingClientRect();
          const text = kind && document.createRange();
          if (text) text.selectNodeContents(kind);
          return {
            text: row.textContent,
            overflow: row.scrollWidth > row.clientWidth + 1,
            textOverlapsPath: !!text && text.getBoundingClientRect().right > path.left + 1,
            errorBelowHeader: !row.classList.contains('error') ||
              result.top >= Math.max(path.bottom, name ? name.bottom : path.bottom) - 1,
            resultInsideRow: result.left >= bounds.left - 1 && result.right <= bounds.right + 1 &&
              result.bottom <= bounds.bottom + 1,
            beforeNextRow: !row.nextElementSibling ||
              bounds.bottom <= row.nextElementSibling.getBoundingClientRect().top + 1
          };
        })""")
        for result in measurements:
            assert not result["overflow"], result
            assert not result["textOverlapsPath"], result
            assert result["errorBelowHeader"], result
            assert result["resultInsideRow"], result
            assert result["beforeNextRow"], result
    finally:
        page.close()
