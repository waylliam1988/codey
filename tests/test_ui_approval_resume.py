"""Parked workers may resume the same durable task and publish one final answer."""
import pytest
from playwright.sync_api import expect

from tests.test_ui_workflow import page as page
from tests.test_ui_workflow import ui_browser as ui_browser


def begin(page):
    return page.evaluate("""() => {
      const run = Requests.begin('a', 'Read a public data endpoint').attempt.runId;
      handleServerEvent({type:'task_start', session_id:'a', run_id:run, mode:'chat'});
      return run;
    }""")


def emit(page, run, kind, **fields):
    page.evaluate('event => handleServerEvent(event)',
                  {'type': kind, 'session_id': 'a', 'run_id': run, 'mode': 'chat', **fields})


@pytest.mark.parametrize('wait', ['approval', 'question'])
def test_resume_keeps_stop_stream_and_final_answer_visible_once(page, wait):
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    run = begin(page)
    emit(page, run, 'task_done', stop_reason=wait, summary='Waiting')
    assert page.evaluate('run => activeSession().terminalRuns.includes(run)', run) is False
    assert page.evaluate('run => Requests.forRun("a",run).attempt.state', run) == 'waiting'
    page.evaluate("""run => applyRunState({busy:false,run_id:run,session_id:'a',active_runs:[],
      last_terminal_event:{type:'task_done',session_id:'a',run_id:run,stop_reason:'approval'}})""", run)
    assert page.evaluate('run => Requests.forRun("a",run).attempt.state', run) == 'waiting'
    # No network call is needed to re-admit the browser projection.
    emit(page, run, 'task_start')
    expect(page.locator('#stop')).to_be_visible()
    emit(page, run, 'reply_stream', stream_id='after-wait', sequence=1, state='partial', text='Received source data')
    expect(page.locator('.msg.asst')).to_contain_text('Received source data')
    for _ in range(2):
        emit(page, run, 'task_done', stop_reason=wait, summary='Waiting')
        emit(page, run, 'task_start')
    emit(page, run, 'reply_stream', stream_id='after-wait', sequence=2, state='complete', text='Verified source answer')
    emit(page, run, 'task_done', stop_reason='done', summary='Verified source answer', final_delivery='success')
    expect(page.locator('.msg.asst')).to_have_count(1)
    expect(page.locator('.msg.asst')).to_contain_text('Verified source answer')
    expect(page.locator('#stop')).to_be_hidden()
    expect(page.locator('.process-title')).to_contain_text('Worked')
    # Replayed terminal events do not hide, duplicate or replace the answer.
    emit(page, run, 'task_done', stop_reason='done', summary='Verified source answer', final_delivery='success')
    emit(page, run, 'task_start')
    expect(page.locator('#stop')).to_be_hidden()
    expect(page.locator('.msg.asst')).to_have_count(1)
    assert not errors, errors


def test_reconnect_snapshot_ignores_old_park_and_stop_targets_resumed_task(page):
    run = begin(page)
    emit(page, run, 'task_done', stop_reason='approval', summary='Waiting')
    # Simulate a cache created by the older client, even without an attempt.
    page.evaluate("""run => {
      activeSession().terminalRuns.push(run);
      UiState.hydrateSessionIndexes(activeSession());
      applyRunState({busy:true,run_id:run,session_id:'a',
        active_runs:[{run_id:run,session_id:'a'}],
        last_terminal_event:{type:'task_done',session_id:'a',run_id:run,stop_reason:'approval'}});
    }""", run)
    expect(page.locator('#stop')).to_be_visible()
    assert page.evaluate('run => Requests.forRun("a",run).attempt.state', run) == 'running'
    assert page.evaluate('run => activeSession().terminalRuns.includes(run)', run) is False
    stops = []
    page.route('**/api/stop', lambda route: (stops.append(route.request.post_data_json), route.fulfill(json={'ok': True})))
    page.locator('#stop').click()
    assert stops == [{'run_id': run}]
    emit(page, run, 'task_done', stop_reason='stopped', summary='Stopped')
    expect(page.locator('#stop')).to_be_hidden()
    emit(page, run, 'task_start')
    expect(page.locator('#stop')).to_be_hidden()


def test_legacy_parked_terminal_cache_does_not_swallow_final_delivery(page):
    run = begin(page)
    emit(page, run, 'task_done', stop_reason='approval')
    page.evaluate("""run => {
      activeSession().terminalRuns.push(run);
      UiState.hydrateSessionIndexes(activeSession());
      applyRunState({busy:true,run_id:run,session_id:'a',active_runs:[{run_id:run,session_id:'a'}]});
    }""", run)
    emit(page, run, 'task_start')
    emit(page, run, 'task_done', stop_reason='done', summary='Recovered final answer', final_delivery='success')
    expect(page.locator('.msg.asst')).to_contain_text('Recovered final answer')
    expect(page.locator('.process-title')).to_contain_text('Worked')


def test_reopening_reads_existing_answer_from_durable_history_despite_old_ui_cache(page):
    durable = page.codey_context.durable
    durable.create_conversation('restored-answer')
    durable.append_message('restored-answer', 'user_message', {'text': 'Read public data'})
    durable.append_message('restored-answer', 'assistant_message', {'text': 'Previously generated durable answer'})
    page.evaluate("""() => {
      const session = UiState.defaultSession(null, 'deepseek');
      session.id = 'restored-answer';
      session.terminalRuns = ['old-parked-run'];
      sessions.push(session);
      switchSession(session.id);
    }""")
    expect(page.locator('.msg.asst')).to_contain_text('Previously generated durable answer')


def test_completed_run_cannot_be_reopened_by_replayed_park(page):
    run = begin(page)
    emit(page, run, 'task_done', stop_reason='approval', event_id=10)
    emit(page, run, 'task_start', event_id=11)
    emit(page, run, 'task_done', stop_reason='done', summary='Final answer', event_id=12)
    page.evaluate('run => applyRunState({busy:false,active_runs:[],last_terminal_event:{type:"task_done",session_id:"a",run_id:run,stop_reason:"approval"}})', run)
    emit(page, run, 'task_start', event_id=11)
    assert page.evaluate('run => Requests.forRun("a",run).attempt.state', run) == 'done'
    expect(page.locator('#stop')).to_be_hidden()
    expect(page.locator('.msg.asst')).to_contain_text('Final answer')


def test_active_snapshot_survives_buffered_older_park(page):
    run = begin(page)
    emit(page, run, 'task_done', stop_reason='approval', event_id=20)
    page.evaluate('run => applyRunState({busy:true,run_id:run,session_id:"a",active_runs:[{run_id:run,session_id:"a"}],last_terminal_event:{type:"task_done",session_id:"a",run_id:run,stop_reason:"approval"}})', run)
    emit(page, run, 'task_done', stop_reason='approval', event_id=20)
    expect(page.locator('#stop')).to_be_visible()
    assert page.evaluate('run => Requests.forRun("a",run).attempt.state', run) == 'running'


@pytest.mark.parametrize('terminal', ['done', 'stopped', 'error'])
def test_versioned_snapshots_and_events_share_an_irreversible_boundary(page, terminal):
    run = begin(page)
    emit(page, run, 'task_done', stop_reason='approval', task_revision=5, event_id=20)
    page.evaluate('run => applyRunState({busy:true,run_id:run,session_id:"a",active_runs:[{run_id:run,session_id:"a"}],task_states:[{run_id:run,session_id:"a",task_revision:8,task_status:"running"}]})', run)
    emit(page, run, 'task_done', stop_reason='approval', task_revision=5, event_id=200)
    expect(page.locator('#stop')).to_be_visible()
    emit(page, run, 'task_done', stop_reason=terminal, summary='Final payload', task_revision=10, event_id=201)
    for state in ('running', 'waiting'):
        page.evaluate('(v) => applyRunState({busy:true,run_id:v.run,session_id:"a",active_runs:[{run_id:v.run,session_id:"a"}],task_states:[{run_id:v.run,session_id:"a",task_revision:11,task_status:v.state,stop_reason:"approval"}]})', {'run':run, 'state':state})
        emit(page, run, 'task_done', stop_reason='approval', task_revision=5, event_id=202)
    expect(page.locator('#stop')).to_be_hidden()
    assert page.evaluate('run => Requests.forRun("a",run).attempt.state', run) == {'done':'done','stopped':'paused','error':'failed'}[terminal]


@pytest.mark.parametrize('wait', ['approval','question','waiting'])
def test_same_version_cached_wait_keeps_snapshot_owner_and_stop(page, wait):
    run = begin(page)
    page.evaluate("""value => applyRunState({busy:true,run_id:value.run,session_id:'a',
      active_runs:[{run_id:value.run,session_id:'a'}],
      task_states:[{run_id:value.run,session_id:'a',task_status:'waiting',task_revision:7,stop_reason:value.wait}],
      last_terminal_event:{type:'task_done',run_id:value.run,session_id:'a',task_status:'waiting',task_revision:7,stop_reason:value.wait}})""", {'run':run,'wait':wait})
    expect(page.locator('#stop')).to_be_visible()
    emit(page,run,'task_done',task_status='waiting',task_revision=7,stop_reason=wait,event_id=20)
    expect(page.locator('#stop')).to_be_visible()


def test_new_version_accepts_small_bus_sequence_after_snapshot(page):
    run = begin(page)
    emit(page,run,'task_done',task_status='waiting',task_revision=5,stop_reason='approval',event_id=1000)
    page.evaluate("""run => applyRunState({busy:true,run_id:run,session_id:'a',active_runs:[{run_id:run,session_id:'a'}],
      task_states:[{run_id:run,session_id:'a',task_status:'running',task_revision:8}]})""",run)
    page.evaluate("run => Requests.fail('a',run,'Confirmation lost')",run)
    emit(page,run,'task_start',task_status='running',task_revision=8,event_id=1)
    assert page.evaluate("run => Requests.forRun('a',run).attempt.state",run) == 'running'
    emit(page,run,'task_done',task_status='completed',task_revision=9,stop_reason='done',event_id=2,summary='Finished')
    expect(page.locator('#stop')).to_be_hidden()
    expect(page.locator('.msg.asst')).to_contain_text('Finished')
