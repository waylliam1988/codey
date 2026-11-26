from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from codey.storage.ui_state_store import MAX_PROJECTS, MAX_SESSIONS
from codey.types.json_value import require_json_integer
from codey.utils.object_fields import require_object_fields, require_object_rows

if TYPE_CHECKING:
    from codey.app.context_ports import ConversationStoreContext


def register_fork(ctx: ConversationStoreContext, source: str, child: str, *, workspace: str = '', preserve_title: bool = False) -> dict[str, object]:
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
