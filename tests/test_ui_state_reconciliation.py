"""Deletion, persisted drafts and task history use the canonical production stores."""
import pytest
from playwright.sync_api import expect

from codey.app import api
from tests.test_ui_workflow import page, select_connection_models, ui_browser  # noqa: F401


def test_deleting_last_chat_preserves_enabled_choice_and_saving_continues(page):  # noqa: F811
    select_connection_models(page, {'id': 'zen', 'label': 'Zen', 'models': [
        {'id': 'beam-free', 'name': 'Beam Free', 'efforts': []},
        {'id': 'glm', 'name': 'GLM', 'efforts': []},
    ]}, selected=['glm'])
    page.evaluate("""async () => {
      const state=UiState.current();state.sessions=state.sessions.filter(s=>s.id==='a');
      state.sessions[0].provider='zen';state.sessions[0].modelSelection={connection_id:'zen',model:'glm',effort:null,efforts:{}};
      UiState.apply(state);UiState.persistNow();await UiState.ensureSaved();renderChat();
    }""")
    statuses = []
    page.on('response', lambda response: statuses.append(response.status) if response.url.endswith('/api/ui_state') else None)
    page.evaluate("deleteSession('a')")
    page.wait_for_function("UiState.current().sessions.length===1 && UiState.current().sessions[0].id!=='a'")
    expect(page.locator('#provider-name')).to_have_text('GLM')
    page.locator('#task').fill('Keep saving after deletion')
    assert page.evaluate('UiState.ensureSaved()') is True
    expect(page.locator('#ui-save-notice')).to_be_hidden()
    assert 409 not in statuses
    assert page.codey_context.load_ui_state()['sessions'][0]['draft']['text'] == 'Keep saving after deletion'


def test_empty_chat_never_implicitly_selects_a_disabled_catalog_entry(page):  # noqa: F811
    select_connection_models(page, {'id': 'zen', 'label': 'Zen', 'models': [
        {'id': 'disabled', 'name': 'Disabled first', 'efforts': []},
        {'id': 'enabled', 'name': 'Enabled', 'efforts': []},
    ]}, selected=['enabled'])
    page.evaluate("() => {const s=activeSession();s.provider='zen';delete s.modelSelection;renderChat();Composer.updateSend();}")
    expect(page.locator('#provider-name')).to_have_text('Choose model')
    assert page.evaluate("ProviderUI.runSelection(activeSession(),'zen').model_selection") is None
    expect(page.locator('#model-notice')).not_to_contain_text('This model is disabled')


def test_new_chat_does_not_inherit_a_disabled_website_model(page):  # noqa: F811
    page.evaluate("""() => {
      const settings=Models.snapshot();settings.preferences.revision++;
      settings.preferences.sources.websites.models=['mimo'];Models.applySettings(settings);
      newSession();
    }""")
    expect(page.locator('#provider-name')).to_have_text('Choose model')
    expect(page.locator('#model-notice')).to_be_hidden()
    assert page.evaluate('activeSession().provider') == ''
    assert page.evaluate('UiState.ensureSaved()') is True
    page.reload()
    expect(page.locator('#provider-name')).to_have_text('Choose model')


def test_history_restores_failed_checkpoint_and_live_checkpoint_through_same_lifecycle(page):  # noqa: F811
    rows = [
        {'id': 'u1', 'type': 'user', 'text': 'First', 'runId': 'old'},
        {'id': 't1', 'type': 'tool_pending', 'kind': 'read', 'path': 'old.py', 'pending': True, 'toolKey': 'old:call', 'runId': 'old'},
        {'id': 'u2', 'type': 'user', 'text': 'Continue', 'runId': 'new'},
        {'id': 't2', 'type': 'tool_pending', 'kind': 'read', 'path': 'new.py', 'pending': True, 'toolKey': 'new:call', 'runId': 'new'},
    ]
    tasks = [
        {'session_id': 'a', 'run_id': 'old', 'task_revision': 9, 'task_status': 'failed', 'stop_reason': 'provider_failure'},
        {'session_id': 'a', 'run_id': 'new', 'task_revision': 2, 'task_status': 'running'},
    ]
    page.route('**/api/sessions/a/messages**', lambda route: route.fulfill(json={
        'schema_version': 1, 'session_id': 'a', 'epoch': 'history', 'snapshot_revision': 10,
        'items': rows, 'task_states': tasks, 'has_more': False,
    }))
    page.evaluate("ConversationUI.refreshHistory('a')")
    expect(page.locator('.process-title')).to_have_text(['Failed', 'Working · read new.py'])
    expect(page.locator('#stop')).to_be_visible()
    # A delayed old start cannot undo the checkpoint-backed failure.
    page.evaluate("handleServerEvent({type:'task_start',session_id:'a',run_id:'old',task_revision:8,task_status:'running'})")
    expect(page.locator('.process-title').first).to_have_text('Failed')
    page.evaluate("applyRunState({busy:false,active_runs:[],task_states:[{session_id:'a',run_id:'new',task_revision:3,task_status:'cancelled'}]})")
    expect(page.locator('.process-title')).to_have_text(['Failed', 'Stopped'])
    expect(page.locator('#stop')).to_be_hidden()


def test_clear_history_keeps_session_and_draft_in_canonical_ui_store(page):  # noqa: F811
    page.locator('#task').fill('Keep my draft')
    assert page.evaluate('UiState.ensureSaved()') is True
    page.evaluate("clearMessages('a')")
    expect(page.locator('#task')).to_have_value('Keep my draft')
    assert page.evaluate('UiState.ensureSaved()') is True
    assert next(row for row in page.codey_context.load_ui_state()['sessions'] if row['id'] == 'a')['draft']['text'] == 'Keep my draft'


@pytest.mark.parametrize('status,title', [('failed', 'Failed'), ('running', 'Work')])
@pytest.mark.parametrize('owner_loaded', [True, False])
def test_inherited_fork_history_never_assigns_source_task_ownership_to_child(page, status, title, owner_loaded):  # noqa: F811
    page.route('**/api/sessions/a/messages**', lambda route: route.fulfill(json={
        'schema_version': 1, 'session_id': 'a', 'epoch': 'history', 'snapshot_revision': 1, 'has_more': False,
        'items': [{'id': 'source-user', 'type': 'user', 'text': 'Inherited', 'runId': 'source'},
                  {'id': 'source-tool', 'type': 'tool', 'kind': 'read', 'path': 'source.py', 'runId': 'source'}],
        'task_states': [{'session_id': 'b', 'run_id': 'source', 'task_revision': 2, 'task_status': status}],
    }))
    page.evaluate("ConversationUI.refreshHistory('a')")
    expect(page.locator('.process-title')).to_have_text(title + ' · read 1 file')
    expect(page.locator('#stop')).to_be_hidden()

    if status == 'running':
        if not owner_loaded:
            page.evaluate("() => {const s=UiState.current();s.sessions=s.sessions.filter(x=>x.id!=='b');UiState.apply(s);}")
        page.evaluate("handleServerEvent({type:'task_done',session_id:'b',run_id:'source',task_revision:3,task_status:'failed',stop_reason:'provider_failure'})")
        expect(page.locator('.process-title')).to_have_text('Failed · read 1 file')
        expect(page.locator('#stop')).to_be_hidden()
        page.evaluate("handleServerEvent({type:'task_start',session_id:'b',run_id:'source',task_revision:2,task_status:'running'})")
        expect(page.locator('.process-title')).to_have_text('Failed · read 1 file')


def test_legacy_draft_survives_boot_read_failure_and_retry(page):  # noqa: F811
    baseline = page.evaluate('UiState.current().revision')
    # Seed the next document after pagehide has atomically cached the old page.
    page.add_init_script("""(() => {
      localStorage.removeItem('codey:ui-cache');
      localStorage.setItem('codey:sessions',JSON.stringify([
        {id:'a',provider:'deepseek',draft:{text:'RECOVER ME',revision:3}},
        {id:'deleted',draft:{text:'Do not resurrect'}}
      ]));
      localStorage.setItem('codey:active','a');
    })();""")
    reads = []
    def unavailable(route):
        if route.request.method == 'GET' and len(reads) < 2:
            reads.append(True)
            route.fulfill(status=503, json={'error': 'temporarily_unavailable'})
        else:
            route.fallback()
    page.route('**/api/ui_state', unavailable)
    page.reload()
    expect(page.locator('#task')).to_have_value('RECOVER ME')
    page.wait_for_function('revision => UiState.current().revision > revision', arg=baseline)
    assert page.evaluate('UiState.ensureSaved()') is True
    saved = page.codey_context.load_ui_state()
    assert next(row for row in saved['sessions'] if row['id'] == 'a')['draft']['text'] == 'RECOVER ME'
    assert not any(row['id'] == 'deleted' for row in saved['sessions'])


def test_lost_creation_response_then_remote_deletion_cannot_resurrect_chat_on_reload(page):  # noqa: F811
    existing = {row['id'] for row in page.codey_context.load_ui_state()['sessions']}
    lost = []
    def save(route):
        if route.request.method == 'POST' and not lost:
            body = route.request.post_data_json
            new = [row['id'] for row in body['state']['sessions'] if row['id'] not in existing]
            if new:
                status, _ = api.save_ui_state_response(page.codey_context, body)
                assert status == 200
                lost.extend(new)
                route.abort()
                return
        route.fallback()
    page.route('**/api/ui_state', save)
    assert page.evaluate('async () => {newSession();return await UiState.ensureSaved()}') is False
    assert len(lost) == 1
    with page.codey_context.ui_state_store_lock:
        page.codey_context.ui_state_store.forget_session(lost[0])
    page.reload()
    page.wait_for_function("id => !UiState.current().sessions.some(s=>s.id===id)", arg=lost[0])
    assert page.evaluate('UiState.ensureSaved()') is True
    assert all(row['id'] != lost[0] for row in page.codey_context.load_ui_state()['sessions'])
