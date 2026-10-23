"""Command grouping and math use the same presentation on live and restored views."""
import pytest
from playwright.sync_api import expect
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


@pytest.mark.parametrize('order', ['early', 'late', 'approval'])
def test_command_stays_in_work_before_answer_across_refresh(page, order):
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.evaluate("""order => {
      const command = {type:'shell_result', id:'weather', runId:'weather-run',
        command:'curl.exe --max-time 20 https://wttr.in/Fuzhou?format=j1',
        approved:true, shellStatus:'exit', exitCode:0, output:'27 C'};
      if(order === 'approval') addToSession('a',{...command,type:'shell_request'});
      if(order === 'early') addToSession('a',command);
      addToSession('a',{type:'asst',runId:'weather-run',finalAnswer:true,text:'Temperature: 27 C'});
      if(order !== 'early') addToSession('a',command);
    }""", order)
    for _ in range(2):
        expect(page.locator('.process-body .command-card')).to_have_count(1)
        expect(page.locator('#chat > .command-card')).to_have_count(0)
        assert page.evaluate("""() => {
          const card=document.querySelector('.command-card');
          const answer=[...document.querySelectorAll('.msg.asst')].find(node=>node.textContent.includes('Temperature:'));
          return !!(card.compareDocumentPosition(answer) & Node.DOCUMENT_POSITION_FOLLOWING);
        }""")
        page.evaluate('() => renderChat()')
    assert not errors, errors


def test_packaged_math_has_styles_and_loadable_fonts(page):
    page.evaluate(r"""() => addToSession('a',{type:'asst',runId:'math-run',finalAnswer:true,
      text:'$C = (F - 32) \\times \\frac{5}{9}$\n\n\\[C = 48 \\times \\frac{5}{9} \\approx 26.67\\]'})""")
    expect(page.locator('.msg.asst math')).to_have_count(2)
    expect(page.locator('.msg.asst .mfrac')).to_have_count(2)
    result = page.evaluate("""async () => {
      await document.fonts.ready;
      const html=document.querySelector('.katex-html');
      const mml=document.querySelector('.katex-mathml');
      const family=getComputedStyle(document.querySelector('.katex')).fontFamily;
      const loaded=await document.fonts.load('16px KaTeX_Main');
      return {family,mathmlPosition:getComputedStyle(mml).position,loaded:loaded.length,htmlWidth:html.getBoundingClientRect().width};
    }""")
    assert 'KaTeX_Main' in result['family']
    assert result['mathmlPosition'] == 'absolute'
    assert result['loaded'] > 0
    assert result['htmlWidth'] > 20


def test_late_replayed_command_events_keep_terminal_result_and_new_turn_separate(page):
    page.evaluate("""() => {
      addToSession('a',{type:'user',text:'Old question'});
      addToSession('a',{type:'asst',runId:'old',text:'Old answer',finalAnswer:true});
      addToSession('a',{type:'user',text:'New question'});
      addToSession('a',{type:'run_state',runId:'new',state:'running'});
      addToSession('a',{type:'shell_result',id:'old-command',runId:'old',executionId:'one',
        command:'curl.exe weather',approved:true,shellStatus:'exit',exitCode:0,output:'27 C'});
      addToSession('a',{type:'run_state',runId:'old',state:'done'});
      window.oldAnswer=[...document.querySelectorAll('.asst')].find(n=>n.dataset.runId==='old');
      for(const status of ['running','queued','exit']) handleServerEvent({type:'shell_result',
        session_id:'a',run_id:'old',id:'old-command',execution_id:'one',command:'curl.exe weather',
        approved:true,status,exit_code:status==='exit'?0:null,output:status==='exit'?'27 C':''});
      addToSession('a',{type:'shell_approval_status',id:'old-command',runId:'old',
        approval:{decision:'allow',execution:'running',continuation:'waiting_for_result'}});
    }""")
    expect(page.locator('.command-card')).to_have_count(1)
    expect(page.locator('.command-card')).to_have_attribute('data-status', 'success')
    expect(page.locator('[data-run-id="old"] .process-title')).to_contain_text('Worked')
    expect(page.locator('[data-run-id="old"] .spinner')).to_have_count(0)
    expect(page.locator('[data-run-id="new"] .process-title')).to_contain_text('Working')
    assert page.evaluate("""() => document.querySelector('[data-run-id="old"].process-block').compareDocumentPosition(window.oldAnswer) & Node.DOCUMENT_POSITION_FOLLOWING""")
    assert page.evaluate('() => window.oldAnswer.isConnected')


def test_results_update_while_answer_streams_and_identical_commands_remain_distinct(page):
    page.evaluate("""() => {
      addToSession('a',{type:'user',text:'Check two sources'});
      handleServerEvent({type:'task_start',session_id:'a',run_id:'stream-run',mode:'chat'});
      for(const id of ['one','two']) handleServerEvent({type:'shell_request',id,
        session_id:'a',run_id:'stream-run',command:'curl.exe weather',cwd:'.'});
      handleServerEvent({type:'reply_stream',session_id:'a',run_id:'stream-run',
        stream_id:'answer',sequence:1,state:'partial',text:'Reading sources'});
    }""")
    expect(page.locator('.msg.asst')).to_contain_text('Reading sources')
    page.evaluate("""() => {
      window.liveAnswer=document.querySelector('.msg.asst');
      for(const id of ['two','one','two']) handleServerEvent({type:'shell_result',id,
        session_id:'a',run_id:'stream-run',command:'curl.exe weather',approved:true,
        status:'exit',exit_code:0,output:id,execution_id:id});
    }""")
    expect(page.locator('.process-body .command-card')).to_have_count(2)
    expect(page.get_by_role('button', name='Allow once', exact=True)).to_have_count(0)
    expect(page.locator('#chat > .command-card')).to_have_count(0)
    assert page.evaluate('() => window.liveAnswer === document.querySelector(".msg.asst")')
    assert page.evaluate("""() => ['one','two'].every(id => {
      const card=document.querySelector('.process-body [data-approval-id="'+id+'"]');
      return !!(card.compareDocumentPosition(window.liveAnswer) & Node.DOCUMENT_POSITION_FOLLOWING);
    })""")
