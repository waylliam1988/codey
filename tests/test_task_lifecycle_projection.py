from contextlib import closing

from codey.app.context import AppContext
from codey.app.event_payloads import browser_event_payload, machine_event_payload
from codey.app.task_lifecycle_projection import version_task_event
from codey.durable.events import committed_task_version
from tests.support.shell_approval import admit_shell_task


def test_checkpoint_revision_is_carried_by_events_and_current_snapshots(tmp_path):
    with closing(AppContext(tmp_path, start_scheduler=False)) as ctx:
        admit_shell_task(ctx, session_id='session', run_id='task')
        ctx.reserve_run(session_id='session', run_id='task', project=None, task='inspect', provider_id='local')
        rows = []
        unsubscribe = ctx.event_bus.observe(lambda _seq, event: rows.append(dict(event)))
        ctx.emit({'type':'task_start','run_id':'task','session_id':'session'})
        started = next(event for event in rows if event['type'] == 'task_start')
        assert started['task_revision'] == ctx.durable.task('task').revision
        parked = ctx.durable.wait_task('task', state={'stop_reason':'approval'})
        event = version_task_event(ctx.durable, {
            'type':'task_done','run_id':'task','session_id':'session','stop_reason':'approval',
        })
        parked_version = committed_task_version(parked, 'task')['task_revision']
        assert event['task_revision'] == parked_version > started['task_revision']
        ctx.finish_run('task', event)
        ctx.durable.cancel_task('task', reason='user stopped')
        snapshot = ctx.run_state_payload()
        current = next(row for row in snapshot['task_states'] if row['run_id'] == 'task')
        assert current['task_revision'] > parked_version
        assert current['task_status'] == 'cancelled'
        # Stored/replayed parks retain their original version after cancellation.
        assert snapshot['last_terminal_event']['task_revision'] == parked_version
        assert version_task_event(ctx.durable, event)['task_revision'] == parked_version
        for projection in (machine_event_payload(event), browser_event_payload(event)):
            assert projection['task_revision'] == parked_version
            assert projection['task_status'] == 'waiting'
        unsubscribe()


def test_edit_does_not_restore_old_pause_or_shell_result_from_registry_snapshot(tmp_path):
    with closing(AppContext(tmp_path, start_scheduler=False)) as ctx:
        ctx.durable.admit_input(conversation_id='chat', request_id='old', task_id='old', text='old', provider_snapshot={})
        source = ctx.durable.task('old').state['input_entry_id']
        ctx.durable.cancel_task('old')
        ctx.run_registry.set_last_terminal_event({'type':'task_done','session_id':'chat','run_id':'old','stop_reason':'stopped'})
        ctx.run_registry.set_last_shell_result({'type':'shell_result','session_id':'chat','run_id':'old','command':'old'})
        ctx.durable.admit_input(conversation_id='chat', request_id='new', task_id='new', text='new', provider_snapshot={},
            task_state={'input':{'kind':'edit_user_message','entry_id':source,'text':'new',
                                 'expected_revision':ctx.durable.store.head('chat')[1]}})
        snapshot = ctx.run_state_payload()
        assert snapshot['last_terminal_event'] is None
        assert snapshot['last_shell_result'] is None
        assert all(row['run_id'] != 'old' for row in snapshot['task_states'])
