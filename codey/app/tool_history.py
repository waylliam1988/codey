"""Persist bounded tool presentation as display-only conversation entries."""
from __future__ import annotations

from typing import Any

from codey.app.event_payloads import browser_event_payload


def record_tool_display(ctx: Any, event: dict[str, Any]) -> None:
    if event.get('type') not in {'tool_started', 'tool'} or not event.get('call_id'):
        return
    session, run = str(event.get('session_id') or ''), str(event.get('run_id') or '')
    if not session or not run or not ctx.durable.has_conversation(session):
        return
    bounded = browser_event_payload(event)
    if bounded is None:
        return
    pending = event['type'] == 'tool_started'
    call = str(bounded.get('call_id') or '')
    payload = {
        'type': 'tool_pending' if pending else 'tool', 'toolKey': f'{run}:{call}',
        'callId': call, 'parentCallId': str(bounded.get('parent_call_id') or ''),
        'runId': run, 'turn': bounded.get('turn', 0), 'pending': pending,
        'kind': bounded.get('kind', ''), 'toolName': bounded.get('tool_name', ''),
        'path': bounded.get('path', ''), 'command': bounded.get('command', ''),
        'activity': bounded.get('activity', ''), 'result': bounded.get('detail', bounded.get('result', '')),
        'preview': bounded.get('preview'), 'status': bounded.get('status', ''),
        'error': bounded.get('status') == 'error', 'changed': bounded.get('changed') is True,
        'outputTruncated': bounded.get('detail_truncated') is True or bounded.get('truncated') is True,
        **({'outputBytes': bounded['output_bytes']} if type(bounded.get('output_bytes')) is int else {}),
        **({'outputStoredBytes': bounded['output_stored_bytes']} if type(bounded.get('output_stored_bytes')) is int else {}),
    }
    ctx.durable.record_display_message(session, run_id=run, identity=f'{call}:{"start" if pending else "final"}', payload=payload)
