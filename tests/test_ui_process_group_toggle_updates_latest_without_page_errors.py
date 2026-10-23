"""Tool/reasoning details toggles must update scrolling through a public API."""

import pytest
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
    expect(group).not_to_have_attribute("open", "")
    group.locator(".process-summary").click()
    expect(group).to_have_attribute("open", "")
    group.locator(".process-summary").click()
    expect(group).not_to_have_attribute("open", "")
    page.wait_for_timeout(100)
    assert errors == []


@pytest.mark.parametrize('expanded', [False, True])
@pytest.mark.parametrize('empty_streams', [0, 4])
def test_completed_work_has_same_compact_answer_gap_after_live_updates(page, expanded, empty_streams):  # noqa: F811
    page.evaluate("""() => {
      addToSession('a', {type:'user',text:'Check the code'});
      addToSession('a', {type:'run_state',runId:'spacing',state:'running'});
    }""")
    group = page.locator('.process-group')
    if expanded:
        group.locator('.process-summary').click()
    page.evaluate(r"""emptyStreams => {
      for(let i=0;i<20;i++) addToSession('a', {type:'tool',runId:'spacing',toolKey:'tool-'+i,kind:'read',path:'file-'+i+'.py',result:'read contents'});
      for(let i=0;i<emptyStreams;i++) addToSession('a', {type:'asst',runId:'spacing',streamId:'empty-'+i,finalAnswer:false,text:'\n\n'});
      addToSession('a', {type:'asst',runId:'spacing',streamId:'answer',finalAnswer:false,text:'The completed answer.'});
      addToSession('a', {type:'run_state',runId:'spacing',state:'done',elapsedMs:120000});
    }""", empty_streams)
    if expanded:
        group.locator('.process-summary').click()
    page.wait_for_timeout(100)
    metrics = page.evaluate("""() => {
      const block=document.querySelector('.process-block'), summary=block.querySelector('.process-summary');
      const answer=document.querySelector('.msg.asst[data-stream-id="answer"] .body');
      return {gap:answer.getBoundingClientRect().top-summary.getBoundingClientRect().bottom,
        block: block.getBoundingClientRect().height, html:document.getElementById('chat').innerHTML.slice(0,400)};
    }""")
    assert metrics['gap'] == pytest.approx(6, abs=1), metrics
