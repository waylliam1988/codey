"""Ghost task policy and post-turn projections.

This module owns Ghost side effects around a task run. The task runtime only
passes it terminal facts; Ghost stores do not need to know the old task entry
implementation exists.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from codey.ghost.learning_loop import (
    DEFAULT_GHOST_LEARNING_NEW_CHAT_TIMEOUT,
    DEFAULT_GHOST_LEARNING_TIMEOUT,
    GhostLearningLoop,
    GhostLearningTurn,
)
from codey.ghost.router import (
    GhostRouteRequest,
    GhostRouteResult,
    GhostRouter,
)
from codey.ghost.work_queue import (
    GhostWorkItem,
    is_strict_work_continuation,
    proof_refs_from_task_event,
)
from codey.knowledge.research_interest import (
    apply_research_affinity_hints,
    build_research_interest_candidates,
)
from codey.operations.context import RunFrame
from codey.operations.ghost_context import ghost_affinity_store
from codey.operations.research_flow import (
    record_research_plan_trace,
    record_research_proof_review_trace,
    research_queue_item_title,
)
from codey.providers import controls as provider_controls
from codey.research.completion_gate import RESEARCH_QUEUE_KINDS, ResearchCompletionGate
from codey.runs.ledger_projection import load_run_projection
from codey.runtime import cancellation


PRODUCTION_GHOST_ROUTER_TIMEOUT = 12.0
PRODUCTION_GHOST_ROUTER_NEW_CHAT_TIMEOUT = 8.0
PRODUCTION_GHOST_ROUTER_ATTEMPTS = 1


@dataclass(frozen=True)
class GhostTaskPolicyDeps:
    state: Any
    run_ledgers: Any = None
    evidence_ledgers: Any = None
    work_checkpoints: Any = None
    knowledge_store: Any = None
    router_provider_factory: Callable[[str], Any] | None = None
    learning_provider_factory: Callable[[str], Any] | None = None
    learning_modes: tuple[str, ...] = ("chat",)
    has_reviewable_diff: Callable[[str | None], bool] | None = None
    record_completion_proof_trace: Callable[[Any, object], None] | None = None


def maybe_claim_work_item(
    deps: GhostTaskPolicyDeps,
    request: Any,
    *,
    run_id: str,
):
    if str(request.intent or "auto").strip().lower() != "auto":
        return None
    if not is_strict_work_continuation(request.task):
        return None
    store = getattr(deps.state, "ghost_work_queue", None)
    if store is None:
        return None
    if not _ghost_learning_enabled(deps.state):
        return None
    try:
        affinity_hints = ()
        affinity_store = ghost_affinity_store(deps.state)
        if affinity_store is not None:
            try:
                queued = store.list_items(
                    status="queued",
                    project=request.project or "",
                    session_id=request.session_id,
                )
                affinity_hints = affinity_store.query_work_priority_hints(
                    queued,
                    project=request.project or "",
                    session_id=request.session_id,
                )
            except Exception:
                affinity_hints = ()
        result = store.claim_next(
            session_id=request.session_id,
            project=request.project or "",
            run_id=run_id,
            user_request=request.task,
            affinity_hints=affinity_hints,
        )
    except Exception:
        return None
    return result if getattr(result, "ok", False) and getattr(result, "item", None) is not None else None


def maybe_route_auto(
    deps: GhostTaskPolicyDeps,
    request: Any,
    *,
    baseline_mode: str,
    run_id: str,
) -> GhostRouteResult | None:
    intent = str(request.intent or "auto").strip().lower()
    if intent != "auto":
        return None
    store = getattr(deps.state, "ghost_router", None)
    if store is None:
        return None
    if not _ghost_learning_enabled(deps.state):
        return None
    provider_factory = deps.router_provider_factory
    if provider_factory is None:
        return None
    route_request = GhostRouteRequest(
        task=request.task,
        baseline_mode=baseline_mode,
        run_id=run_id,
        session_id=request.session_id,
        project=request.project or "",
        provider_id=request.provider_id,
        continue_request=request.continue_task,
        has_reviewable_diff=deps.has_reviewable_diff(request.project) if deps.has_reviewable_diff else False,
    )
    try:
        with provider_controls.suppress_assistance():
            return GhostRouter(store).route(
                route_request,
                provider_factory=provider_factory,
                timeout=PRODUCTION_GHOST_ROUTER_TIMEOUT,
                new_chat_timeout=PRODUCTION_GHOST_ROUTER_NEW_CHAT_TIMEOUT,
                max_attempts=PRODUCTION_GHOST_ROUTER_ATTEMPTS,
            )
    except cancellation.TaskCancelled:
        raise
    except Exception:
        return None


def release_work_item(
    deps: GhostTaskPolicyDeps,
    item: GhostWorkItem | None,
    *,
    run_id: str,
    reason: str,
) -> None:
    if item is None:
        return
    store = getattr(deps.state, "ghost_work_queue", None)
    if store is None:
        return
    try:
        store.release_item(item.id, run_id=run_id, reason=reason)
    except Exception:
        return


def run_ghost_post_turn(
    deps: GhostTaskPolicyDeps,
    frame: RunFrame | None,
    event: dict[str, object],
    item: GhostWorkItem | None,
    *,
    research_result: Any = None,
    project_text: str = "",
) -> None:
    complete_or_block_work_item(deps, frame, event, item, research_result=research_result)
    if frame is None:
        sync_affinity_terminal_event(
            deps,
            event,
            request=None,
            run_id=str(event.get("run_id") or ""),
            project_text=project_text,
        )
        return
    maybe_run_learning(deps, frame, event)
    maybe_sync_continuity(deps, frame, event)
    maybe_sync_work_queue(deps, frame, event)
    maybe_kick_sleep(deps, frame, event)


def maybe_run_learning(
    deps: GhostTaskPolicyDeps,
    frame: RunFrame,
    event: dict[str, object],
) -> None:
    if deps.learning_provider_factory is None:
        return
    mode = str(event.get("mode") or "")
    if mode not in deps.learning_modes:
        return
    if str(event.get("stop_reason") or "") != "done":
        return
    try:
        loop = GhostLearningLoop(
            signal_store=getattr(deps.state, "ghost_signals", None),
            inbox_store=getattr(deps.state, "ghost_inbox", None),
            hebbian_store=getattr(deps.state, "ghost_hebbian", None),
        )
        result = loop.learn_from_turn(
            GhostLearningTurn(
                mode=mode,
                user_text=frame.request.task,
                assistant_text=str(event.get("summary") or ""),
                session_id=frame.request.session_id,
                run_id=frame.run_id,
                project=frame.project_text if mode != "chat" else "",
                provider_id=frame.provider_id,
            ),
            provider_factory=deps.learning_provider_factory,
            timeout=DEFAULT_GHOST_LEARNING_TIMEOUT,
            new_chat_timeout=DEFAULT_GHOST_LEARNING_NEW_CHAT_TIMEOUT,
        )
        deps.state.emit(result.to_event(run_id=frame.run_id, session_id=frame.request.session_id))
    except Exception:
        return


def maybe_sync_continuity(
    deps: GhostTaskPolicyDeps,
    frame: RunFrame,
    event: dict[str, object],
) -> None:
    store = getattr(deps.state, "ghost_continuity", None)
    if store is None:
        return
    mode = str(event.get("mode") or "")
    if mode not in {"chat", "planning"}:
        return
    if not _ghost_learning_enabled(deps.state):
        try:
            deps.state.emit({
                "type": "ghost_continuity_done",
                "run_id": frame.run_id,
                "session_id": frame.request.session_id,
                "ok": True,
                "skipped_reason": "learning_disabled",
                "items_changed": 0,
                "total_items": 0,
                "warnings": [],
            })
        except Exception:
            pass
        return
    try:
        projection = _run_projection(deps, frame.request.session_id, frame.run_id)
        result = store.sync_from_sources(
            hebbian_store=getattr(deps.state, "ghost_hebbian", None),
            run_projection=projection,
            knowledge_store=deps.knowledge_store,
            user_focus_excerpt=frame.request.task,
            session_id=frame.request.session_id,
            run_id=frame.run_id,
            project=frame.project_text if mode != "chat" else "",
            mode=mode,
        )
        deps.state.emit(result.to_event(run_id=frame.run_id, session_id=frame.request.session_id))
    except Exception:
        return


def maybe_kick_sleep(
    deps: GhostTaskPolicyDeps,
    frame: RunFrame,
    event: dict[str, object],
) -> None:
    if str(event.get("stop_reason") or "") != "done":
        return
    kick = getattr(deps.state, "kick_ghost_sleep", None)
    if not callable(kick):
        return
    try:
        kick(
            trigger="post_turn",
            run_id=frame.run_id,
            session_id=frame.request.session_id,
            project=frame.project_text,
            run_projection=_run_projection(deps, frame.request.session_id, frame.run_id),
        )
    except Exception:
        return


def maybe_sync_work_queue(
    deps: GhostTaskPolicyDeps,
    frame: RunFrame,
    event: dict[str, object],
) -> None:
    store = getattr(deps.state, "ghost_work_queue", None)
    affinity_store = ghost_affinity_store(deps.state)
    if store is None and affinity_store is None:
        return
    if not _ghost_learning_enabled(deps.state):
        return
    try:
        projection = _run_projection(deps, frame.request.session_id, frame.run_id)
        research_interest_candidates = build_research_interest_candidates(
            deps.knowledge_store,
            session_id=frame.request.session_id,
            project=frame.project_text,
        )
        if affinity_store is not None and research_interest_candidates:
            hints = affinity_store.query_research_priority_hints(
                research_interest_candidates,
                session_id=frame.request.session_id,
                project=frame.project_text,
            )
            research_interest_candidates = apply_research_affinity_hints(
                research_interest_candidates,
                hints,
            )
        if store is not None:
            store.sync_from_sources(
                continuity_store=getattr(deps.state, "ghost_continuity", None),
                work_checkpoint_store=deps.work_checkpoints,
                run_projection=projection,
                terminal_event=event,
                research_interest_candidates=research_interest_candidates,
                session_id=frame.request.session_id,
                run_id=frame.run_id,
                project=frame.project_text,
            )
        if affinity_store is not None:
            affinity_store.sync_from_sources(
                hebbian_store=getattr(deps.state, "ghost_hebbian", None),
                work_queue_store=store,
                research_interest_candidates=research_interest_candidates,
                router_store=getattr(deps.state, "ghost_router", None),
                run_projection=projection,
                terminal_event=event,
                session_id=frame.request.session_id,
                project=frame.project_text,
            )
    except Exception:
        return


def sync_affinity_terminal_event(
    deps: GhostTaskPolicyDeps,
    event: dict[str, object],
    *,
    request: Any | None,
    run_id: str,
    project_text: str,
) -> None:
    affinity_store = ghost_affinity_store(deps.state)
    if affinity_store is None:
        return
    session_id = str(event.get("session_id") or getattr(request, "session_id", "") or "")
    try:
        affinity_store.sync_from_sources(
            hebbian_store=getattr(deps.state, "ghost_hebbian", None),
            work_queue_store=getattr(deps.state, "ghost_work_queue", None),
            router_store=getattr(deps.state, "ghost_router", None),
            run_projection=_run_projection(deps, session_id, run_id),
            terminal_event=event,
            session_id=session_id,
            project=project_text,
        )
    except Exception:
        return


def complete_or_block_work_item(
    deps: GhostTaskPolicyDeps,
    frame: RunFrame | None,
    event: dict[str, object],
    item: GhostWorkItem | None,
    *,
    research_result: Any = None,
) -> None:
    if item is None:
        return
    store = getattr(deps.state, "ghost_work_queue", None)
    if store is None:
        return
    try:
        run_id = str(event.get("run_id") or getattr(item, "started_run_id", "") or "")
        if frame is None:
            if str(event.get("stop_reason") or "") != "done":
                store.block_item(
                    item.id,
                    run_id=run_id,
                    blocked_reason=str(event.get("stop_reason") or "run_not_done"),
                )
            return
        projection = _run_projection(deps, frame.request.session_id, frame.run_id)
        if str(event.get("stop_reason") or "") == "done":
            if str(getattr(item, "kind", "") or "") in RESEARCH_QUEUE_KINDS:
                decision = ResearchCompletionGate(deps.evidence_ledgers).evaluate(
                    item=item,
                    event=event,
                    research_result=research_result,
                    session_id=frame.request.session_id,
                    project=frame.project_text,
                )
                if deps.record_completion_proof_trace is not None:
                    deps.record_completion_proof_trace(frame.trace, decision.proof)
                if decision.complete:
                    store.complete_item(
                        item.id,
                        run_id=frame.run_id,
                        proof_refs=decision.proof_refs,
                    )
                else:
                    record_research_proof_review_trace(frame.trace, decision.review)
                    record_research_plan_trace(
                        frame.trace,
                        decision.review,
                        question=research_queue_item_title(item) or frame.request.task,
                    )
                    store.block_item(
                        item.id,
                        run_id=frame.run_id,
                        blocked_reason=decision.blocked_reason or "research_proof_failed",
                    )
            else:
                store.complete_item(
                    item.id,
                    run_id=frame.run_id,
                    proof_refs=proof_refs_from_task_event(
                        item,
                        event,
                        run_projection=projection,
                    ),
                )
        else:
            store.block_item(
                item.id,
                run_id=frame.run_id,
                blocked_reason=str(event.get("stop_reason") or "run_not_done"),
            )
    except Exception:
        return


def _ghost_learning_enabled(state: Any) -> bool:
    inbox_store = getattr(state, "ghost_inbox", None)
    if inbox_store is None:
        return True
    try:
        return bool(inbox_store.learning_enabled())
    except Exception:
        return False


def _run_projection(deps: GhostTaskPolicyDeps, session_id: str, run_id: str):
    if deps.run_ledgers is None:
        return None
    return load_run_projection(deps.run_ledgers, session_id, run_id)


__all__ = [
    "GhostTaskPolicyDeps",
    "complete_or_block_work_item",
    "maybe_sync_work_queue",
    "maybe_claim_work_item",
    "maybe_route_auto",
    "release_work_item",
    "run_ghost_post_turn",
    "sync_affinity_terminal_event",
]
