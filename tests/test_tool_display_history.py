from __future__ import annotations

from codey.app.context import AppContext
from codey.runtime.core.models import ToolCall
from codey.runtime.observe.events import RunEvent, run_event_ui_payload
from codey.toolchain.runtime import ToolOutcome


def test_tool_identity_and_parent_survive_cold_history_without_changing_model_context(tmp_path):
    ctx = AppContext(tmp_path, start_scheduler=False)
    try:
        ctx.durable.admit_input(conversation_id='chat', request_id='one', task_id='run_one', text='original instruction', provider_snapshot={})
        before = ctx.durable.context_snapshot('chat', token_budget=10000).messages
        call = ToolCall('read', {'path': 'a.py'}, call_id='child-1')
        start = RunEvent.tool_started(1, call, 'Reading')
        start.metadata['parent_call_id'] = 'parent-1'
        end = RunEvent.tool_finished(1, call, ToolOutcome('line one\nline two', True))
        end.metadata['parent_call_id'] = 'parent-1'
        for event in (start, end, start, end):
            ctx.emit(run_event_ui_payload('run_one', 'chat', event))
        assert len(ctx.durable.entries('chat')) == 3
        page = ctx.durable.history_page('chat')
        rows = [item for item in page['items'] if item['type'] in {'tool_pending', 'tool'}]
        assert [item['type'] for item in rows] == ['tool_pending', 'tool']
        assert all(item['callId'] == 'child-1' and item['parentCallId'] == 'parent-1' for item in rows)
        assert rows[-1]['result'] == 'line one\nline two'
        assert ctx.durable.context_snapshot('chat', token_budget=10000).messages == before
    finally:
        ctx.close()
    restarted = AppContext(tmp_path, start_scheduler=False)
    try:
        assert restarted.durable.history_page('chat')['items'][-1]['callId'] == 'child-1'
    finally:
        restarted.close()
