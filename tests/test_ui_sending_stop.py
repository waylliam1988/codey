"""Unconfirmed HTTP admission remains cancellable without a second task."""
from playwright.sync_api import expect
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def hold_admission(page):
    page.evaluate("""() => {
      window.stopCalls=[];
      const real=fetch;
      window.fetch=(url,init) => {
        if(url==='/api/stop') { stopCalls.push(JSON.parse(init.body).run_id); return Promise.resolve(new Response('{}')); }
        if(url==='/api/run') {
          window.sentRun=JSON.parse(init.body).run_id;
          return new Promise((resolve,reject) => {
            window.lateAck=()=>resolve(new Response(JSON.stringify({run_id:sentRun})));
            init.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')),{once:true});
          });
        }
        return real(url,init);
      };
    }""")
    page.locator('#task').fill('A slow network request')
    page.locator('#send').click()
    page.wait_for_function('!!window.sentRun')


def test_sending_is_stoppable_and_late_ack_does_not_revive_it(page):
    hold_admission(page)
    expect(page.locator('#stop')).to_be_visible()
    page.locator('#stop').click()
    page.wait_for_function('!Composer.isSending()')
    page.wait_for_function('Requests.forRun("a",sentRun).attempt.state === "paused"')
    assert page.evaluate('stopCalls') == [page.evaluate('sentRun')]
    page.evaluate('lateAck()')
    expect(page.locator('#stop')).to_be_hidden()
    assert page.evaluate('Requests.forRun("a",sentRun).attempt.state') == 'paused'
    # A task accepted just before Stop uses the same cancellation identity.
    page.evaluate("handleServerEvent({type:'task_start',session_id:'a',run_id:sentRun,mode:'chat'})")
    page.wait_for_function('stopCalls.length === 2')
    assert len(set(page.evaluate('stopCalls'))) == 1


def test_admission_timeout_is_not_infinite_or_automatically_retried(page):
    # Native AbortSignal.timeout uses browser active time, not Playwright's
    # mocked clock. Control only this native deadline signal deterministically.
    page.evaluate("""() => {
      const nativeTimeout=AbortSignal.timeout.bind(AbortSignal);
      window.admissionDeadline=new AbortController();
      AbortSignal.timeout=ms => ms===30000 ? admissionDeadline.signal : nativeTimeout(ms);
    }""")
    hold_admission(page)
    page.evaluate("admissionDeadline.abort(new DOMException('Timed out','TimeoutError'))")
    page.wait_for_function('!Composer.isSending()')
    expect(page.locator('.request-status')).to_contain_text('Message delivery could not be confirmed')
    expect(page.get_by_role('button',name='Check status',exact=True)).to_be_visible()
    expect(page.get_by_role('button',name='Retry',exact=True)).to_be_disabled()
    expect(page.locator('#stop')).to_be_visible()
    page.locator('#stop').click()
    page.wait_for_function('Requests.forRun("a",sentRun).attempt.state === "paused"')
    expect(page.locator('#stop')).to_be_hidden()


def test_stop_targets_sending_chat_not_another_chats_running_task(page):
    page.evaluate("handleServerEvent({type:'task_start',session_id:'b',run_id:'other-chat',mode:'chat'})")
    hold_admission(page)
    page.locator('#stop').click()
    page.wait_for_function('stopCalls.length === 1')
    assert page.evaluate('stopCalls[0] === sentRun')
    assert page.evaluate('stopCalls.includes("other-chat")') is False


def test_confirmed_execution_clears_unknown_admission_and_allows_next_send(page):
    page.evaluate("""() => {
      const run=Requests.begin('a','Delivery pending').attempt.runId;
      Requests.fail('a',run,'Delivery unknown',{sendFailure:'admission_unconfirmed',runOwner:'old'});
      handleServerEvent({type:'task_start',session_id:'a',run_id:run,task_revision:1,task_status:'running'});
      const attempt=Requests.forRun('a',run).attempt;
      if(attempt.sendFailure || attempt.error || attempt.runOwner) throw new Error('Stale admission metadata');
      handleServerEvent({type:'task_done',session_id:'a',run_id:run,task_revision:2,task_status:'completed',stop_reason:'done',summary:'Completed'});
    }""")
    page.locator('#task').fill('Next request')
    expect(page.locator('#send')).to_be_enabled()
    expect(page.locator('#stop')).to_be_hidden()
    expect(page.get_by_role('button',name='Check status',exact=True)).to_have_count(0)
