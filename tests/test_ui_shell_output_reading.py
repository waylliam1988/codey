"""Quiet command controls preserve retained output and the user's reading position."""
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def test_shell_output_scrolls_without_extra_controls_and_survives_result_updates(page):
    page.evaluate(r"""() => {
      window.shellText=Array.from({length:100},(_,i)=>`Line ${i}: `+'retained output '.repeat(24)).join('\n');
      addToSession('a',{type:'user',text:'Check the output'});
      addToSession('a',{type:'shell_result',id:'reading',runId:'reader',executionId:'exec-reader',
        approved:true,shellStatus:'exit',exitCode:0,command:'python logs.py',output:window.shellText});
      document.querySelector('.process-group').open=true;
      document.querySelector('.command-card > details').open=true;
    }""")
    output = page.locator('.shell-stdout')
    expect(output).to_be_visible()
    page.wait_for_function("""() => {
      const output=document.querySelector('.shell-stdout');
      return output.scrollHeight-output.scrollTop-output.clientHeight<4;
    }""")
    expect(page.locator('.command-panel-label,.command-notice')).to_have_count(0)
    assert 'Show last' not in page.locator('.command-card').inner_text()
    expect(page.locator('.command-card').get_by_role('button', name='Saved permissions', exact=True)).to_have_count(0)
    expect(page.get_by_role('button', name='Back to latest output')).to_be_hidden()
    output.focus()
    assert output.evaluate('e => getComputedStyle(e).outlineStyle') == 'none'
    assert output.evaluate('e => getComputedStyle(e,"::-webkit-scrollbar-corner").backgroundColor') == 'rgba(0, 0, 0, 0)'
    output.evaluate("""e => {
      e.dispatchEvent(new WheelEvent('wheel',{deltaY:-100}));
      e.scrollTop=80; e.scrollLeft=35; e.dispatchEvent(new Event('scroll'));
    }""")
    expect(page.get_by_role('button', name='Back to latest output')).to_be_visible()
    page.evaluate(r"""() => addToSession('a',{type:'shell_result',id:'reading',runId:'reader',executionId:'exec-reader',
      approved:true,shellStatus:'exit',exitCode:0,command:'python logs.py',output:window.shellText+'\nLate retained output'})""")
    expect(output).to_contain_text('Late retained output')
    page.wait_for_function("""() => {
      const output=document.querySelector('.shell-stdout');
      return output.scrollTop===80 && output.scrollLeft===35;
    }""")
    page.locator('.command-card > details > summary').click()
    page.evaluate(r"""() => addToSession('a',{type:'shell_result',id:'reading',runId:'reader',executionId:'exec-reader',
      approved:true,shellStatus:'exit',exitCode:0,command:'python logs.py',output:window.shellText+'\nMore output while collapsed'})""")
    page.locator('.command-card > details > summary').click()
    page.wait_for_function("""() => {
      const output=document.querySelector('.shell-stdout');
      return output.scrollTop===80 && output.scrollLeft===35;
    }""")
    page.get_by_role('button', name='Back to latest output').click()
    expect(page.get_by_role('button', name='Back to latest output')).to_be_hidden()
    assert output.inner_text().startswith('Line 0:')
    expect(page.locator('.command-status')).to_have_text('✓ Success')


def test_truncated_output_and_copy_keep_original_vectors_and_no_bright_scroll_corner(page):
    page.evaluate(r"""() => {
      addToSession('a',{type:'user',text:'Review retained output'});
      addToSession('a',{type:'shell_result',id:'truncated',runId:'retained',approved:true,
        shellStatus:'exit',exitCode:7,command:'python probe.py',outputTruncated:true,
        output:'retained line\n'.repeat(90)});
      document.querySelector('.process-group').open=true;
      document.querySelector('.command-card > details').open=true;
    }""")
    expect(page.locator('.shell-stdout')).to_contain_text('[output truncated]')
    expect(page.locator('.command-status')).to_have_text('Exit code 7')
    expect(page.locator('.command-notice')).to_have_count(0)
    copy = page.get_by_role('button', name='Copy retained output', exact=True)
    page.locator('.shell-stdout').hover()
    expect(copy).to_be_visible()
    assert copy.locator('svg').get_attribute('viewBox') == '0 0 21 21'
    assert copy.locator('svg').get_attribute('fill') == 'currentColor'
    page.set_viewport_size({'width': 680, 'height': 720})
    assert page.locator('.command-panel').evaluate('e=>e.scrollWidth<=e.clientWidth')


def test_live_output_follows_only_until_the_user_reads_up_and_stderr_is_not_duplicated(page):
    text = '\n'.join(f'Line {index}: retained content' for index in range(100))
    snapshot = {'revision': 1, 'execution_state': 'running', 'stdout': {'text': text}, 'stderr': {'text': ''}}

    def output_response(route):
        query = parse_qs(urlsplit(route.request.url).query)
        assert query['session_id'] == ['a']
        assert query['run_id'] == ['live-run']
        if int(query['after_revision'][0]) >= snapshot['revision']:
            route.fulfill(status=304, body='')
        else:
            route.fulfill(json=snapshot)

    page.route('**/api/executions/live-output/output?*', output_response)
    page.evaluate("""() => {
      addToSession('a',{type:'user',text:'Read live output'});
      addToSession('a',{type:'shell_result',id:'live-card',sessionId:'a',runId:'live-run',
        executionId:'live-output',approved:true,shellStatus:'running',command:'python live.py'});
      document.querySelector('.process-group').open=true;
      document.querySelector('.command-card > details').open=true;
    }""")
    output = page.locator('.shell-stdout')
    expect(output).to_contain_text('Line 99:')
    page.wait_for_function("""() => {const e=document.querySelector('.shell-stdout');
      return e.scrollHeight-e.scrollTop-e.clientHeight<4;}""")
    output.evaluate("""e => {e.dispatchEvent(new WheelEvent('wheel',{deltaY:-80}));
      e.scrollTop=60; e.dispatchEvent(new Event('scroll'));}""")
    snapshot.update(revision=2, stdout={'text': text + '\nNew live line'})
    expect(output).to_contain_text('New live line')
    expect(output).to_have_js_property('scrollTop', 60)
    page.get_by_role('button', name='Back to latest output').click()
    snapshot.update(revision=3, execution_state='settled', exit_code=-7,
                    stdout={'text': text + '\nFinal live line', 'truncated': True}, stderr={'text': 'One warning'})
    expect(output).to_contain_text('Final live line')
    expect(output).to_contain_text('[output truncated]')
    assert output.inner_text().count('One warning') == 1
    expect(page.locator('.command-status')).to_have_text('Exit code -7')
    expect(page.locator('.command-card')).to_have_attribute('data-status', 'failed')
    expect(page.get_by_role('button', name='Back to latest output')).to_be_hidden()
