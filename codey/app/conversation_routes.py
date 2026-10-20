"""Conversation branch controls; head changes are explicit, revision fenced and idle only."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from codey.durable.errors import CommitConflict
from codey.storage.ui_state_store import MAX_SESSIONS


def _validate_context_tip(ctx: Any, session: str, entry_id: str) -> None:
    pending: set[str] = set()
    for entry in ctx.durable.context(session, leaf_id=entry_id):
        if entry.kind == 'tool_call':
            calls = entry.payload.get('tool_calls', (entry.payload,))
            for call in calls:
                if isinstance(call, Mapping) and isinstance(call.get('id'), str):
                    pending.add(call['id'])
        elif entry.kind == 'tool_result':
            pending.discard(str(entry.payload.get('call_id') or ''))
    if pending:
        raise ValueError('This point has unfinished tool calls. Choose an earlier message or a settled workflow checkpoint.')


def register_fork(ctx: Any, source: str, child: str) -> dict[str, Any]:
    # Register with the canonical UI store so an admitted fork remains discoverable after reload.
    with ctx.ui_state_store_lock:
        state: dict[str, Any] = ctx.ui_state_store.load()
        if not any(session['id'] == child for session in state['sessions']):
            if len(state['sessions']) >= MAX_SESSIONS:
                raise ValueError('Conversation list is full. Remove a conversation before creating a branch.')
            parent: dict[str, Any] = next((session for session in state['sessions'] if session['id'] == source), {})
            session = {key: parent[key] for key in ('provider', 'modelSelection', 'projectId', 'research') if key in parent}
            session.update({'id': child, 'title': ('Fork · ' + str(parent.get('title') or 'Chat'))[:80], 'messages': []})
            state['sessions'].insert(0, session)
            state = ctx.ui_state_store.save(state, base_revision=state['revision'])
        return state


def conversation_tree_response(ctx: Any, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    session = str((query.get('session_id') or [''])[0])
    try:
        target = str((query.get('entry_id') or [''])[0])
        leaf = str((query.get('leaf_id') or [''])[0])
        if target:
            return 200, ctx.durable.history_window(session, target, leaf_id=leaf or None)
        tree = ctx.durable.conversation_tree(session)
        related = []
        for candidate in ctx.load_ui_state()['sessions']:
            identity = candidate['id']
            if not ctx.durable.has_conversation(identity):
                continue
            ancestors = ctx.durable.conversation_lineage(identity)
            if identity == session or any(parent['session_id'] == session for parent in ancestors):
                related.append({'session_id': identity, 'title': candidate['title'], 'ancestors': ancestors})
        tree['related_sessions'] = related
        return 200, tree
    except (KeyError, ValueError):
        return 404, {'ok': False, 'error': 'conversation_path_not_found'}


def conversation_action_response(ctx: Any, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    session, entry, action = body.get('session_id'), body.get('entry_id'), body.get('action')
    if not isinstance(session, str) or not session or not isinstance(entry, str) or not entry:
        return 400, {'ok': False, 'error': 'conversation and entry required'}
    try:
        _validate_context_tip(ctx, session, entry)
        if action == 'select':
            revision = body.get('revision')
            if type(revision) is not int:
                return 400, {'ok': False, 'error': 'conversation revision required'}
            # The mutation line prevents admission racing between the idle check and head change.
            ctx.durable.select_idle_branch(session, entry, expected_revision=revision)
            return 200, ctx.durable.conversation_tree(session)
        if action == 'fork':
            request = body.get('request_id')
            if not isinstance(request, str) or not 1 <= len(request) <= 200:
                return 400, {'ok': False, 'error': 'fork request identity required'}
            identity = hashlib.sha256(f'{len(session)}:{session}{entry}:{request}'.encode()).hexdigest()[:32]
            child = 'fork_' + identity
            if not ctx.durable.has_conversation(child):
                ctx.durable.fork(session, entry, child_conversation_id=child)
            state = register_fork(ctx, session, child)
            return 200, {'ok': True, 'session_id': child, 'state': state}
        return 400, {'ok': False, 'error': 'unknown conversation action'}
    except (KeyError, ValueError, CommitConflict) as exc:
        return 409, {'ok': False, 'error': str(exc)}
