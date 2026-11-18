"""Add bounded local preferences and goals to normal shared-kernel entries."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.directive import build_ghost_directive
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.observation_index import render_retrieved_block
from codey.ghost.observations import GhostObservationStore
from codey.operations.kernel_contracts import KernelExecutionDeps
from codey.operations.project_goals import project_goal_sources
from codey.policies.permissions import allows_context_source
from codey.runtime.effects.effect_records import RuntimeEffectError
from codey.workspace.context_admission import GhostContextSource, request_scope, sources_from_values

SourceValidator = Callable[[tuple[GhostContextSource, ...]], bool]


@dataclass(frozen=True)
class PersonalContext:
    text: str
    sources: tuple[GhostContextSource, ...]
    validate: SourceValidator | None


def _read(home: Path, *, project: str, session_id: str, run_id: str, task: str,
          profile: str, existing: frozenset[str]) -> tuple[GhostContextSource, ...]:
    try:
        inbox = GhostInboxStore(home)
        if not all(inbox.control_enabled(control, project=project, session_id=session_id)
                   for control in ("retrieve_projections", "admit_hints")):
            return ()
        result: list[GhostContextSource] = []
        if "ghost_directive" not in existing and allows_context_source(profile, "ghost_directive"):
            directive = build_ghost_directive(GhostHebbianStore(home), project=project, session_id=session_id,
                                               affinity_store=GhostAffinityStore(home))
            result.extend(sources_from_values(directive=directive.text, scope=request_scope(project, session_id)))
        if project and "ghost_experiences" not in existing and allows_context_source(profile, "ghost_experiences"):
            observations = GhostObservationStore(home)
            history = observations.retrieve(task, project=project, session_id=session_id,
                                            scope="project", exclude_run_id=run_id)
            if history:
                result.extend(sources_from_values(experiences=render_retrieved_block(history),
                                                   scope=request_scope(project, session_id)))
        goal_keys = ("ghost_project_goal_history", "ghost_confirmed_project_goal")
        if not existing.intersection(goal_keys) and all(allows_context_source(profile, key) for key in goal_keys):
            result.extend(project_goal_sources(home, project=project, session_id=session_id,
                                                task=task, exclude_run_id=run_id))
        return tuple(result)
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        return ()


def supplement_personal_context(*, text: str, sources: tuple[GhostContextSource, ...],
                                scope: str, run_id: str, task: str, enabled: bool,
                                execution: KernelExecutionDeps,
                                validate: SourceValidator | None) -> PersonalContext:
    """Use the entry's durable local authority; never guess a global state home."""
    original = PersonalContext(text, sources, validate)
    sink = execution.intent_sink
    # Project adapters own their complete source snapshot and fault fallback.
    # Do not create a second route around their validator.
    if not enabled or sink is None or validate is not None:
        return original
    # A Chat's physical tool workspace is not its logical project identity.
    # Use the admitted task, which also owns the session and deletion receipt.
    try:
        task_record = sink.durable.task(run_id)
    except KeyError:
        return original
    logical_project = task_record.state.get("project")
    if logical_project is not None and not isinstance(logical_project, str):
        return original
    if task_record.conversation_id != sink.session_id:
        return original
    project = logical_project or ""
    if scope != request_scope(project, sink.session_id):
        return original
    try:
        home = sink.managed_outputs.root.parent
    except RuntimeEffectError:
        return original
    existing = frozenset(source.key for source in sources)

    def read() -> tuple[GhostContextSource, ...]:
        return _read(home, project=project, session_id=sink.session_id, run_id=run_id, task=task,
                     profile=execution.permission_profile, existing=existing)

    added = read()
    if not added:
        return original
    combined = (*sources, *added)

    def still_valid(actual: tuple[GhostContextSource, ...]) -> bool:
        return actual == combined and read() == added

    return PersonalContext("\n\n".join(part for part in (text, *(source.text for source in added)) if part),
                           combined, still_valid)
