"""Continue belongs to a stopped task, independently of transient browser attempts."""
import pytest
from playwright.sync_api import expect
from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def stopped_history(page):
    page.evaluate("""() => {
      addToSession('a',{type:'user',runId:'old-task',entryId:'old-input',text:'Assess this project'});
      handleServerEvent({type:'task_done',session_id:'a',run_id:'old-task',task_status:'cancelled',task_revision:5,stop_reason:'stopped',turns:1});
      addToSession('a',{type:'user',runId:'new-task',entryId:'new-input',text:'Another question',attempts:[{runId:'unrelated-attempt',state:'done'}]});
      handleServerEvent({type:'task_done',session_id:'a',run_id:'new-task',task_status:'completed',task_revision:8,stop_reason:'max_turns',turns:2});
    }""")
    expect(page.get_by_role('button',name='Continue',exact=True)).to_have_count(2)


@pytest.mark.parametrize('index,source', [(0,'old-task'),(1,'new-task')])
def test_continue_uses_clicked_task_after_durable_history_replaced_attempts(page,index,source):
    stopped_history(page)
    bodies=[]
    def submit(route):
        body=route.request.post_data_json
        bodies.append(body)
        route.fulfill(json={'ok':True,'run_id':body['run_id']})
    page.route('**/api/run',submit)
    page.get_by_role('button',name='Continue',exact=True).nth(index).click()
    page.wait_for_function('!Composer.isSending()')
    assert len(bodies)==1
    assert bodies[0]['input']['kind']=='resume_run'
    assert bodies[0]['input']['source_run_id']==source
    assert bodies[0]['input']['instruction']
    assert 'task' not in bodies[0]
    assert not page.locator('.info-line').filter(has_text='continue task').count()
    assert not page.get_by_text('No source run is available to resume.',exact=True).count()


def test_continue_cannot_double_submit_and_stop_targets_new_admission(page):
    stopped_history(page)
    pending=[]
    stops=[]
    page.route('**/api/run',lambda route:pending.append(route))
    def stop(route):
        stops.append(route.request.post_data_json['run_id'])
        route.fulfill(json={'ok':True})
    page.route('**/api/stop',stop)
    button=page.get_by_role('button',name='Continue',exact=True).first
    button.evaluate('(button)=>{button.click();button.click()}')
    page.wait_for_function('Composer.isSending()')
    expect(page.locator('#stop')).to_be_visible()
    assert len(pending)==1
    new_run=pending[0].request.post_data_json['run_id']
    page.locator('#stop').click()
    page.wait_for_function('!Composer.isSending()')
    assert stops==[new_run]
    assert new_run!='old-task'


def test_history_without_task_identity_does_not_offer_a_guessed_continue(page):
    page.evaluate("addToSession('a',{type:'pause',text:'Historical pause without an owner'})")
    expect(page.get_by_role('button',name='Continue',exact=True)).to_have_count(0)



def test_approved_task_remains_stoppable_and_continues_after_history_rehydration(page):
    page.evaluate("""() => {
      window.approvedRun=Requests.begin('a','Assess this project').attempt.runId;
      handleServerEvent({type:'task_start',session_id:'a',run_id:approvedRun,task_status:'running',task_revision:1});
      handleServerEvent({type:'shell_request',session_id:'a',run_id:approvedRun,id:'approval',command:'python -c "print(1)"',cwd:'.'});
      applyRunState({busy:true,run_id:approvedRun,session_id:'a',active_runs:[{run_id:approvedRun,session_id:'a'}],
        task_states:[{run_id:approvedRun,session_id:'a',task_revision:3,task_status:'waiting',stop_reason:'approval'}],
        last_terminal_event:{type:'task_done',run_id:approvedRun,session_id:'a',task_revision:3,task_status:'waiting',stop_reason:'approval'}});
      handleServerEvent({type:'shell_result',session_id:'a',run_id:approvedRun,id:'approval',command:'python -c "print(1)"',approved:true,status:'exit',exit_code:0,output:'1'});
      handleServerEvent({type:'task_start',session_id:'a',run_id:approvedRun,task_status:'running',task_revision:4});
    }""")
    expect(page.locator('#stop')).to_be_visible()
    stops=[]
    def stop(route):
        stops.append(route.request.post_data_json['run_id'])
        route.fulfill(json={'ok':True})
    page.route('**/api/stop',stop)
    page.locator('#stop').click()
    assert stops==[page.evaluate('approvedRun')]
    page.evaluate("""() => {
      handleServerEvent({type:'task_done',session_id:'a',run_id:approvedRun,task_status:'cancelled',task_revision:5,stop_reason:'stopped'});
      const session=sessions.find(s=>s.id==='a');
      session.messages=session.messages.map(m=>m.type==='user'?{type:'user',entryId:'durable-input',runId:approvedRun,text:m.text}:m);
      UiState.hydrateSessionIndexes(session);
      renderChat();
    }""")
    expect(page.locator('#stop')).to_be_hidden()
    bodies=[]
    def submit(route):
        body=route.request.post_data_json
        bodies.append(body)
        route.fulfill(json={'ok':True,'run_id':body['run_id']})
    page.route('**/api/run',submit)
    page.get_by_role('button',name='Continue',exact=True).click()
    page.wait_for_function('!Composer.isSending()')
    assert len(bodies)==1
    assert bodies[0]['input']['source_run_id']==page.evaluate('approvedRun')
    assert bodies[0]['run_id']!=page.evaluate('approvedRun')
