"""Conversation branch controls; head changes are explicit, revision fenced and idle only."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from codey.durable.errors import CommitConflict
from codey.storage.ui_state_store import MAX_PROJECTS, MAX_SESSIONS


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


def register_fork(ctx: Any, source: str, child: str, *, workspace: str = '', preserve_title: bool = False) -> dict[str, Any]:
    # Register with the canonical UI store so an admitted fork remains discoverable after reload.
    with ctx.ui_state_store_lock:
        state: dict[str, Any] = deepcopy(ctx.ui_state_store.load())
        if not any(session['id'] == child for session in state['sessions']):
            if len(state['sessions']) >= MAX_SESSIONS:
                raise ValueError('Conversation list is full. Remove a conversation before creating a branch.')
            parent: dict[str, Any] = next((session for session in state['sessions'] if session['id'] == source), {})
            if not parent:
                raise ValueError('The source chat was removed. No new sidebar entry was created.')
            session = {key: parent[key] for key in ('provider', 'modelSelection', 'projectId', 'research') if key in parent}
            if workspace:
                if len(state['projects']) >= MAX_PROJECTS:
                    raise ValueError('Project list is full. Remove a project before creating a worktree.')
                project: dict[str, Any] = next((p for p in state['projects'] if p['id'] == parent.get('projectId')), {})
                project_id = f'worktree_{child}'
                state['projects'].append({'id': project_id, 'name': f"{project.get('name') or 'Project'} · worktree",
                                          'path': workspace, 'expanded': True})
                session['projectId'] = project_id
            title = str(parent.get('title') or 'Chat')
            session.update({'id': child, 'title': (title if preserve_title else 'Fork · ' + title)[:160], 'messages': []})
            state['sessions'].insert(0, session)
            state = ctx.ui_state_store.save(state, base_revision=state['revision'])
        return state


def _turn_tip(ctx: Any, session: str, entry_id: str) -> str:
    path = list(ctx.durable.context(session))
    index = next((i for i, entry in enumerate(path) if entry.entry_id == entry_id), None)
    if index is None:
        raise ValueError('This message is no longer on the current branch. Open its retained branch first.')
    target = path[index]
    if target.kind not in {'user_message', 'assistant_message'}:
        raise ValueError('Choose a user message or a completed answer.')
    tip = target
    for entry in path[index + 1:]:
        if entry.kind == 'user_message' and (target.by_task_id is None or entry.by_task_id != target.by_task_id):
            break
        if target.by_task_id and entry.by_task_id and entry.by_task_id != target.by_task_id:
            break
        if entry.kind in {'user_message', 'assistant_message', 'tool_call', 'tool_result'}:
            tip = entry
    active = ctx.active_run_for(session_id=session)
    if active and target.by_task_id == active.run_id:
        raise ValueError('Wait for this turn to finish before forking it.')
    return str(tip.entry_id)


def fork_response(ctx: Any, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    from codey.app.fork_workspaces import capabilities, service

    identity = str((query.get('session_id') or [''])[0])
    state = ctx.load_ui_state()
    session = next((s for s in state['sessions'] if s['id'] == identity), None)
    # Worktree jobs may be awaiting registration in the sidebar.
    managed = identity.startswith('fork_') and len(identity) == 37 and all(c in '0123456789abcdef' for c in identity[5:])
    job = service(ctx).status(identity) if managed and ctx.state_home is not None else None
    if session is None and job is None:
        return 404, {'ok': False, 'error': 'Chat not found.'}
    origin: list[dict[str, object]] = ctx.durable.conversation_lineage(identity)[:1] if ctx.durable.has_conversation(identity) else []
    result: dict[str, Any] = {'ok': True, 'origin': origin[0] if origin else None, 'job': job}
    if (query.get('capabilities') or [''])[0] == '1':
        project = next((p for p in state['projects'] if p['id'] == (session or {}).get('projectId')), None)
        result.update(capabilities(str(project['path']) if project else ''))
    if job and job['status'] == 'ready':
        result.update(session_id=identity, state=state)
    return 200, result


def retry_fork_response(ctx: Any, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from codey.app.fork_workspaces import service

    try:
        child = str(body.get('session_id') or '')
        with ctx.fork_request_lock:
            status = service(ctx).status(child)
            if not status:
                raise ValueError('Worktree setup was not found.')
            job = service(ctx).start(status['source'], child, status['entry'], status['workspace'])
            return 202, {'ok': True, 'session_id': child, 'job': job}
    except (OSError, ValueError) as error:
        return 409, {'ok': False, 'error': str(error)}


def conversation_tree_response(ctx: Any, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    session = str((query.get('session_id') or [''])[0])
    try:
        if (query.get('navigation') or [''])[0] == '1':
            return 200, ctx.durable.navigation_page(session, before=(query.get('before') or [''])[0] or None)
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
        if action == 'select':
            _validate_context_tip(ctx, session, entry)
            revision = body.get('revision')
            if type(revision) is not int:
                return 400, {'ok': False, 'error': 'conversation revision required'}
            # The mutation line prevents admission racing between the idle check and head change.
            ctx.durable.select_idle_branch(session, entry, expected_revision=revision)
            return 200, ctx.durable.conversation_tree(session)
        if action == 'fork':
            with ctx.fork_request_lock:
                return _fork_action(ctx, body, session, entry)
        return 400, {'ok': False, 'error': 'unknown conversation action'}
    except (KeyError, ValueError, CommitConflict) as exc:
        return 409, {'ok': False, 'error': str(exc)}


def _fork_action(ctx: Any, body: dict[str, Any], session: str, entry: str) -> tuple[int, dict[str, Any]]:
    request = body.get('request_id')
    if not isinstance(request, str) or not 1 <= len(request) <= 200:
        return 400, {'ok': False, 'error': 'fork request identity required'}
    identity = hashlib.sha256(f'{len(session)}:{session}{entry}:{request}'.encode()).hexdigest()[:32]
    child = 'fork_' + identity
    destination = body.get('destination', 'local')
    if destination not in ('local', 'worktree') or body.get('boundary', 'entry') not in ('entry', 'turn'):
        raise ValueError('Invalid fork destination or boundary.')
    if not ctx.durable.has_conversation(child):
        if body.get('boundary') == 'turn':
            entry = _turn_tip(ctx, session, entry)
        _validate_context_tip(ctx, session, entry)
        ctx.durable.fork(session, entry, child_conversation_id=child)
    if destination == 'worktree':
        from codey.app.fork_workspaces import service

        current = ctx.load_ui_state()
        parent: dict[str, Any] = next((s for s in current['sessions'] if s['id'] == session), {})
        project = next((p for p in current['projects'] if p['id'] == parent.get('projectId')), None)
        if not project:
            raise ValueError('A Git project is required to fork in a new worktree.')
        if any(s['id'] == child for s in current['sessions']) and not service(ctx).status(child):
            raise ValueError('This request already created a chat in the current workspace.')
        job = service(ctx).start(session, child, entry, str(project['path']))
        return 202, {'ok': True, 'session_id': child, 'job': job, 'state': ctx.load_ui_state()}
    # A worktree retry must not silently rebind the accepted child to its source.
    if ctx.state_home is not None:
        from codey.app.fork_workspaces import service

        if service(ctx).status(child):
            raise ValueError('This fork request belongs to a worktree. Retry that destination.')
    state = register_fork(ctx, session, child, preserve_title=body.get('boundary') == 'turn')
    return 200, {'ok': True, 'session_id': child, 'state': state}
