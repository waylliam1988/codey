"""Conversation branch controls; head changes are explicit, revision fenced and idle only."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING

from codey.durable.errors import CommitConflict
from codey.storage.ui_state_store import MAX_PROJECTS, MAX_SESSIONS
from codey.types.json_value import require_json_integer, require_json_string
from codey.utils.object_fields import mapping_fields, require_object_fields, require_object_rows, sequence_items

if TYPE_CHECKING:
    from codey.app.context import AppContext


def _validate_context_tip(ctx: AppContext, session: str, entry_id: str) -> None:
    pending: set[str] = set()
    for entry in ctx.durable.context(session, leaf_id=entry_id):
        if entry.kind == 'tool_call':
            calls: object = entry.payload.get('tool_calls', (entry.payload,))
            if not sequence_items(calls):
                raise ValueError('Tool call records must be a sequence.')
            for call in calls:
                if mapping_fields(call) and isinstance(identity := call.get('id'), str):
                    pending.add(identity)
        elif entry.kind == 'tool_result':
            pending.discard(str(entry.payload.get('call_id') or ''))
    if pending:
        raise ValueError('This point has unfinished tool calls. Choose an earlier message or a settled workflow checkpoint.')


def register_fork(ctx: AppContext, source: str, child: str, *, workspace: str = '', preserve_title: bool = False) -> dict[str, object]:
    # Register with the canonical UI store so an admitted fork remains discoverable after reload.
    with ctx.ui_state_store_lock:
        raw_state: object = ctx.ui_state_store.load()
        state = deepcopy(require_object_fields(raw_state))
        sessions, projects = require_object_rows(state['sessions']), require_object_rows(state['projects'])
        if not any(session['id'] == child for session in sessions):
            if len(sessions) >= MAX_SESSIONS:
                raise ValueError('Conversation list is full. Remove a conversation before creating a branch.')
            parent = next((session for session in sessions if session['id'] == source), dict[str, object]())
            if not parent:
                raise ValueError('The source chat was removed. No new sidebar entry was created.')
            session = {key: parent[key] for key in ('provider', 'modelSelection', 'projectId', 'research', 'browser') if key in parent}
            if workspace:
                if len(projects) >= MAX_PROJECTS:
                    raise ValueError('Project list is full. Remove a project before creating a worktree.')
                project = next((p for p in projects if p['id'] == parent.get('projectId')), dict[str, object]())
                project_id = f'worktree_{child}'
                projects.append({'id': project_id, 'name': f"{project.get('name') or 'Project'} · worktree",
                                          'path': workspace, 'expanded': True})
                session['projectId'] = project_id
            title = str(parent.get('title') or 'Chat')
            session.update({'id': child, 'title': (title if preserve_title else 'Fork · ' + title)[:160], 'messages': list[dict[str, object]]()})
            sessions.insert(0, session)
            state['sessions'], state['projects'] = sessions, projects
            raw_saved: object = ctx.ui_state_store.save(state, base_revision=require_json_integer(state['revision'], 'UI revision'))
            state = require_object_fields(raw_saved)
        return state


def _turn_tip(ctx: AppContext, session: str, entry_id: str) -> str:
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
    return tip.entry_id


def fork_response(ctx: AppContext, query: dict[str, list[str]]) -> tuple[int, dict[str, object]]:
    from codey.app.fork_workspaces import capabilities, service

    identity = str((query.get('session_id') or [''])[0])
    state = ctx.load_ui_state()
    session = next((s for s in require_object_rows(state['sessions']) if s['id'] == identity), None)
    # Worktree jobs may be awaiting registration in the sidebar.
    managed = identity.startswith('fork_') and len(identity) == 37 and all(c in '0123456789abcdef' for c in identity[5:])
    raw_job: object = None
    if managed and ctx.state_home is not None:
        raw_job = service(ctx).status(identity)
    job = require_object_fields(raw_job) if raw_job is not None else None
    if session is None and job is None:
        return 404, {'ok': False, 'error': 'Chat not found.'}
    origin: list[dict[str, object]] = ctx.durable.conversation_lineage(identity)[:1] if ctx.durable.has_conversation(identity) else []
    result: dict[str, object] = {'ok': True, 'origin': origin[0] if origin else None, 'job': job}
    if (query.get('capabilities') or [''])[0] == '1':
        project = next((p for p in require_object_rows(state['projects'])
                        if p['id'] == (session or dict[str, object]()).get('projectId')), None)
        result.update(capabilities(str(project['path']) if project else ''))
    if job and job['status'] == 'ready':
        result.update(session_id=identity, state=state)
    return 200, result


def retry_fork_response(ctx: AppContext, body: Mapping[str, object]) -> tuple[int, dict[str, object]]:
    from codey.app.fork_workspaces import service

    try:
        child = str(body.get('session_id') or '')
        with ctx.fork_request_lock:
            raw_status: object = service(ctx).status(child)
            status = require_object_fields(raw_status) if raw_status is not None else None
            if not status:
                raise ValueError('Worktree setup was not found.')
            raw_job: object = service(ctx).start(require_json_string(status['source'], 'source conversation'), child,
                                                require_json_string(status['entry'], 'fork entry'),
                                                require_json_string(status['workspace'], 'workspace'))
            return 202, {'ok': True, 'session_id': child, 'job': require_object_fields(raw_job)}
    except (OSError, ValueError) as error:
        return 409, {'ok': False, 'error': str(error)}


def conversation_tree_response(ctx: AppContext, query: dict[str, list[str]]) -> tuple[int, dict[str, object]]:
    session = str((query.get('session_id') or [''])[0])
    try:
        if (query.get('navigation') or [''])[0] == '1':
            raw_navigation: object = ctx.durable.navigation_page(session, before=(query.get('before') or [''])[0] or None)
            return 200, require_object_fields(raw_navigation)
        target = str((query.get('entry_id') or [''])[0])
        leaf = str((query.get('leaf_id') or [''])[0])
        if target:
            raw_window: object = ctx.durable.history_window(session, target, leaf_id=leaf or None)
            return 200, require_object_fields(raw_window)
        raw_tree: object = ctx.durable.conversation_tree(session)
        tree = require_object_fields(raw_tree)
        related: list[dict[str, object]] = []
        for candidate in require_object_rows(ctx.load_ui_state()['sessions']):
            identity = require_json_string(candidate['id'], 'session ID')
            if not ctx.durable.has_conversation(identity):
                continue
            ancestors = ctx.durable.conversation_lineage(identity)
            if identity == session or any(parent['session_id'] == session for parent in ancestors):
                related.append({'session_id': identity, 'title': candidate['title'], 'ancestors': ancestors})
        tree['related_sessions'] = related
        return 200, tree
    except ValueError as exc:
        if str(exc) == 'history snapshot changed':
            return 409, {'ok': False, 'error': str(exc)}
        return 404, {'ok': False, 'error': 'conversation_path_not_found'}
    except KeyError:
        return 404, {'ok': False, 'error': 'conversation_path_not_found'}


def conversation_action_response(ctx: AppContext, body: Mapping[str, object]) -> tuple[int, dict[str, object]]:
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
            raw_tree: object = ctx.durable.conversation_tree(session)
            return 200, require_object_fields(raw_tree)
        if action == 'fork':
            with ctx.fork_request_lock:
                return _fork_action(ctx, body, session, entry)
        return 400, {'ok': False, 'error': 'unknown conversation action'}
    except (KeyError, ValueError, CommitConflict) as exc:
        return 409, {'ok': False, 'error': str(exc)}


def _fork_action(ctx: AppContext, body: Mapping[str, object], session: str, entry: str) -> tuple[int, dict[str, object]]:
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
        sessions = require_object_rows(current['sessions'])
        parent = next((s for s in sessions if s['id'] == session), dict[str, object]())
        project = next((p for p in require_object_rows(current['projects']) if p['id'] == parent.get('projectId')), None)
        if not project:
            raise ValueError('A Git project is required to fork in a new worktree.')
        if any(s['id'] == child for s in sessions):
            existing_job: object = service(ctx).status(child)
            if not existing_job:
                raise ValueError('This request already created a chat in the current workspace.')
        raw_job: object = service(ctx).start(session, child, entry, str(project['path']))
        return 202, {'ok': True, 'session_id': child, 'job': require_object_fields(raw_job), 'state': ctx.load_ui_state()}
    # A worktree retry must not silently rebind the accepted child to its source.
    if ctx.state_home is not None:
        from codey.app.fork_workspaces import service

        existing: object = service(ctx).status(child)
        if existing:
            raise ValueError('This fork request belongs to a worktree. Retry that destination.')
    state = register_fork(ctx, session, child, preserve_title=body.get('boundary') == 'turn')
    return 200, {'ok': True, 'session_id': child, 'state': state}
