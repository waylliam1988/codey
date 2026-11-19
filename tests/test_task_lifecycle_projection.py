from contextlib import closing
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.app.event_payloads import browser_event_payload, machine_event_payload
from codey.app.task_lifecycle_projection import version_task_event
from codey.durable.events import committed_task_version
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    decode_json_value,
    require_json_integer,
    require_json_string,
)
from tests.support.shell_approval import admit_shell_task


def records(value: object) -> list[JsonObject]:
    rows = decode_json_value(value)
    if not isinstance(rows, list):
        raise ValueError("expected records")
    return [decode_json_object(row) for row in rows]


def test_checkpoint_revision_is_carried_by_events_and_current_snapshots(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as ctx:
        admit_shell_task(ctx, session_id='session', run_id='task')
        ctx.reserve_run(session_id='session', run_id='task', project=None, task='inspect', provider_id='local')
        rows: list[JsonObject] = []
        unsubscribe = ctx.event_bus.observe(lambda _seq, event: rows.append(decode_json_object(event)))
        ctx.emit({'type':'task_start','run_id':'task','session_id':'session'})
        started = next(event for event in rows if event['type'] == 'task_start')
        assert started['task_revision'] == ctx.durable.task('task').revision
        parked = ctx.durable.wait_task('task', state={'stop_reason':'approval'})
        event = version_task_event(ctx.durable, {
            'type':'task_done','run_id':'task','session_id':'session','stop_reason':'approval',
        })
        parked_version = require_json_integer(committed_task_version(parked, 'task')['task_revision'], 'task_revision')
        assert event['task_revision'] == parked_version > require_json_integer(started['task_revision'], 'task_revision')
        ctx.finish_run('task', dict(event))
        ctx.durable.cancel_task('task', reason='user stopped')
        snapshot = decode_json_object(ctx.run_state_payload())
        current = next(row for row in records(snapshot['task_states']) if row['run_id'] == 'task')
        assert require_json_integer(current['task_revision'], 'task_revision') > parked_version
        assert current['task_status'] == 'cancelled'
        # Stored/replayed parks retain their original version after cancellation.
        assert decode_json_object(snapshot['last_terminal_event'])['task_revision'] == parked_version
        assert version_task_event(ctx.durable, event)['task_revision'] == parked_version
        for projection in (machine_event_payload(event), browser_event_payload(event)):
            assert projection is not None
            assert projection['task_revision'] == parked_version
            assert projection['task_status'] == 'waiting'
        unsubscribe()


def test_edit_does_not_restore_old_pause_or_shell_result_from_registry_snapshot(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as ctx:
        ctx.durable.admit_input(conversation_id='chat', request_id='old', task_id='old', text='old', provider_snapshot={})
        source = ctx.durable.task('old').state['input_entry_id']
        ctx.durable.cancel_task('old')
        ctx.run_registry.set_last_terminal_event({'type':'task_done','session_id':'chat','run_id':'old','stop_reason':'stopped'})
        ctx.run_registry.set_last_shell_result({'type':'shell_result','session_id':'chat','run_id':'old','command':'old'})
        ctx.durable.admit_input(conversation_id='chat', request_id='new', task_id='new', text='new', provider_snapshot={},
            task_state={'input':{'kind':'edit_user_message','entry_id':source,'text':'new',
                                 'expected_revision':ctx.durable.store.head('chat')[1]}})
        snapshot = decode_json_object(ctx.run_state_payload())
        assert snapshot['last_terminal_event'] is None
        assert snapshot['last_shell_result'] is None
        assert all(row['run_id'] != 'old' for row in records(snapshot['task_states']))


@pytest.mark.parametrize('status', ['cancelled', 'failed', 'completed'])
def test_history_reads_terminal_checkpoint_without_volatile_run_registry(tmp_path: Path, status: str) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as ctx:
        ctx.durable.admit_input(conversation_id='chat', request_id='request', task_id='task', text='inspect', provider_snapshot={})
        ctx.durable.record_display_message('chat', run_id='task', identity='tool',
            payload={'type':'tool_pending','runId':'task','pending':True,'kind':'read','path':'file.py'})
        if status == 'cancelled':
            ctx.durable.cancel_task('task', reason='user stopped')
        elif status == 'failed':
            ctx.durable.fail_task('task', 'HTTP 429', state={'stop_reason':'provider_failure'})
        else:
            ctx.durable.complete_task('task', 'Answer', state={})
        assert not ctx.run_state_payload()['active_runs']
        page = ctx.durable.history_page('chat', limit=2)
        assert page['task_states'] == [{
            'run_id':'task','session_id':'chat','task_revision':ctx.durable.task('task').revision,
            'task_status':status,'stop_reason':'provider_failure' if status == 'failed' else '',
            'execution_kind':'model','owner_task_id':None,
        }]
        entry = require_json_string(records(page['items'])[-1]['id'], 'entry id')
        assert ctx.durable.history_window('chat', entry)['task_states'] == page['task_states']


def test_restored_task_can_be_stopped_using_checkpoint_when_registry_has_no_owner(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as ctx:
        ctx.durable.admit_input(conversation_id='chat', request_id='request', task_id='task', text='inspect', provider_snapshot={})
        assert not ctx.active_runs()
        before = records(ctx.durable.history_page('chat')['task_states'])[0]
        assert before['task_status'] == 'pending'
        ctx.request_stop('task')
        after = records(ctx.durable.history_page('chat')['task_states'])[0]
        assert after['task_status'] == 'cancelled'
        assert require_json_integer(after['task_revision'], 'task_revision') > require_json_integer(before['task_revision'], 'task_revision')


def test_unconfigured_empty_chat_does_not_create_default_execution_configuration(tmp_path: Path) -> None:
    from codey.app.control_conversations import import_ui

    with closing(AppContext(tmp_path, start_scheduler=False)) as ctx:
        ctx.save_ui_state({'active_id':'blank','sessions':[{'id':'blank','provider':''}],'projects':[]}, base_revision=0)
        import_ui(ctx)
        assert not ctx.durable.has_conversation('blank')
        assert records(decode_json_object(ctx.load_ui_state())['sessions'])[0]['provider'] == ''
