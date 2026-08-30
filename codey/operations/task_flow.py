"""Task orchestration independent of HTTP request handling."""

from __future__ import annotations

from dataclasses import replace
import uuid
from pathlib import Path
from typing import Any, Callable

from codey.task.model import TaskContract, TaskSubmission
from codey.operations.conversation_plan import build_conversation_plan
from codey.operations.context import RunFrame, RunHooks, RunWork
from codey.operations.chat import run_chat_mode
from codey.operations.project_completion_flow import (
    MAX_COMPLETION_REPAIR_ROUNDS,
    ProjectCompletionDeps,
    handle_project_tool_event,
    record_completion_proof_trace as _record_completion_proof_trace,
    record_review_input_prepared_trace,
    run_project_mode,
)
from codey.operations.provider_preflight import (
    connect_provider_with_preflight,
)
from codey.operations.prompting import (
    record_local_context_trace as _record_local_context_trace,
)
from codey.operations.research_flow import (
    ResearchFlowDeps,
    default_research_search_provider as _default_research_search_provider,
    record_evidence_ledger_write,
    record_research_plan_trace as _record_research_plan_trace,
    record_research_proof_review_trace as _record_research_proof_review_trace,
    research_queue_item_title as _research_queue_item_title,
    run_hybrid_mode,
    run_research_mode,
    run_research_pipeline,
)
from codey.operations.result import ModeOutcome
from codey.task.kind import (
    resolve_task_kind as _resolve_task_kind,
    startup_failover_mode as _startup_failover_mode,
    trace_mode as _trace_mode,
    ui_mode as _ui_mode,
)
from codey.runtime import cancellation
from codey.runtime.task_runtime import TaskRuntime
from codey.providers import controls as provider_controls, flow as provider_flow
from codey.completion.edit_scope import changed_paths_from_changes
from codey.runtime.events import RunEvent, render_run_event, run_event_ui_payload
from codey.runtime.execution_evidence import ExecutionEvidence
from codey.runtime.outcome import OperationOutcome
from codey.runtime.terminalizer import (
    nonnegative_event_count,
    operation_outcome_from_task_done_event,
    task_done_event,
    terminal_turns,
)
from codey.ghost.continuity import build_ghost_continuity
from codey.ghost.directive import build_ghost_directive
from codey.ghost.learning_loop import (
    DEFAULT_GHOST_LEARNING_NEW_CHAT_TIMEOUT,
    DEFAULT_GHOST_LEARNING_TIMEOUT,
    GhostLearningLoop,
    GhostLearningTurn,
)
from codey.ghost.router import (
    DEFAULT_GHOST_ROUTER_NEW_CHAT_TIMEOUT,
    DEFAULT_GHOST_ROUTER_TIMEOUT,
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
from codey.knowledge.store import KnowledgeStore
from codey.storage.managed_outputs import ManagedOutputStore
from codey.workspace.facts import ProjectFactsStore
from codey.workspace.config import (
    ProjectConfigLoadResult,
    load_project_config,
    preferred_provider_for,
)
from codey.workspace.task_context import ProjectTaskContextBuilder
from codey.providers import PROVIDER_LABELS
from codey.providers.capabilities import rank_providers
from codey.providers.diagnostics import ProviderActionError, ProviderFailure
from codey.runtime.prompt_envelope import (
    FailOpenPromptTrace,
    PromptEnvelopeSection,
)
from codey.runs.ledger import RunLedgerStore, RunLedgerWriter
from codey.runs.ledger_projection import (
    build_task_receipt_from_projection,
    load_run_projection,
)
from codey.runs.trace import RunTraceStore
from codey.research.completion_gate import RESEARCH_QUEUE_KINDS, ResearchCompletionGate
from codey.research.evidence_ledger import EvidenceLedgerStore
from codey.reviews.core import has_reviewable_changes
from codey.reviews.impact_map import safe_review_impact_map
from codey.policies.shell_risk import classify_shell_risk
from codey.runs.work_checkpoint import (
    WorkCheckpoint,
    WorkCheckpointStore,
)
from codey.runtime.effects import (
    RuntimeOperationStore,
    RuntimeOperationTransitionError,
    mark_terminal,
)


PRODUCTION_GHOST_ROUTER_TIMEOUT = 12.0
PRODUCTION_GHOST_ROUTER_NEW_CHAT_TIMEOUT = 8.0
PRODUCTION_GHOST_ROUTER_ATTEMPTS = 1


def _render_review_only_summary(review: object) -> str:
    approved = bool(getattr(review, "approved", False))
    summary = str(getattr(review, "summary", "") or "").strip()
    if approved:
        return f"Review approved: {summary or 'No issues found.'}"
    lines = [f"Review requested changes: {summary or 'Issues found.'}"]
    findings = getattr(review, "findings", ()) or ()
    for index, finding in enumerate(list(findings)[:8], start=1):
        path = str(getattr(finding, "path", "") or "").strip()
        issue = str(getattr(finding, "issue", "") or "").strip()
        fix = str(getattr(finding, "suggested_fix", "") or "").strip()
        prefix = f"{index}. "
        if path:
            prefix += f"{path}: "
        text = issue or "Issue found"
        if fix:
            text += f" Suggested fix: {fix}"
        lines.append(prefix + text)
    return "\n".join(lines)


class TaskFlow:
    """Coordinate one task while leaving transport and storage outside."""

    def __init__(
        self,
        state,
        *,
        agent_run: Callable,
        collect_changes: Callable,
        run_review: Callable,
        capture_provider_failure: Callable,
        run_consensus: Callable | None = None,
        run_project_audit: Callable | None = None,
        run_research_advisors: Callable | None = None,
        project_facts: ProjectFactsStore | None = None,
        work_checkpoints: WorkCheckpointStore | None = None,
        run_ledgers: RunLedgerStore | None = None,
        run_traces: RunTraceStore | None = None,
        evidence_ledgers: EvidenceLedgerStore | None = None,
        managed_outputs: ManagedOutputStore | None = None,
        knowledge_store: KnowledgeStore | None = None,
        search_factory: Callable[[], object] | None = None,
        is_git_repository: Callable[[str | Path], bool] | None = None,
        review_fix_turns: int = 12,
        review_log_lines: int = 80,
        ghost_learning_provider_factory: Callable[[str], Any] | None = None,
        ghost_learning_modes: tuple[str, ...] = ("chat",),
        ghost_router_provider_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.state = state
        self.agent_run = agent_run
        self.collect_changes = collect_changes
        self.run_review = run_review
        self.run_consensus = run_consensus
        self.run_project_audit = run_project_audit
        self.run_research_advisors = run_research_advisors
        self.capture_provider_failure = capture_provider_failure
        self.project_facts = project_facts
        self.work_checkpoints = work_checkpoints
        self.run_ledgers = run_ledgers
        self.run_traces = run_traces
        self.runtime_operations: RuntimeOperationStore | None = getattr(
            state,
            "runtime_operations",
            None,
        )
        self.evidence_ledgers = evidence_ledgers
        self.managed_outputs = managed_outputs
        self.knowledge_store = knowledge_store
        self.search_factory = search_factory or _default_research_search_provider
        self.is_git_repository = is_git_repository or (lambda _project: False)
        self.review_fix_turns = review_fix_turns
        self.review_log_lines = review_log_lines
        self.ghost_learning_provider_factory = ghost_learning_provider_factory
        self.ghost_learning_modes = tuple(str(item or "").strip() for item in ghost_learning_modes)
        self.ghost_router_provider_factory = ghost_router_provider_factory
        runtime_log = getattr(state, "runtime_log", None)
        self.runtime = (
            TaskRuntime(
                runtime_log,
                self._run_impl,
                prepare=self._prepare_runtime_submission,
                on_unstarted_failure=self._release_runtime_submission,
            )
            if runtime_log is not None
            else None
        )

    def _research_deps(self) -> ResearchFlowDeps:
        return ResearchFlowDeps(
            state=self.state,
            knowledge_store=self.knowledge_store,
            evidence_ledgers=self.evidence_ledgers,
            search_factory=self.search_factory,
            run_research_advisors=self.run_research_advisors,
            ghost_continuity=self._ghost_continuity,
        )

    def _prepare_runtime_submission(
        self,
        request: TaskSubmission,
    ) -> TaskSubmission | None:
        if request.run_id:
            active = self.state.current_run()
            if active is None:
                reserved = self.state.reserve_run(
                    session_id=request.session_id,
                    project=request.project,
                    task=request.task,
                    provider_id=request.provider_id,
                    run_id=request.run_id,
                )
                return request if reserved is not None else None
            return request
        reserved = self.state.reserve_run(
            session_id=request.session_id,
            project=request.project,
            task=request.task,
            provider_id=request.provider_id,
        )
        if reserved is None:
            return None
        return replace(request, run_id=reserved.run_id)

    def _release_runtime_submission(self, request: TaskSubmission) -> None:
        if request.run_id:
            try:
                self.state.release_run(request.run_id)
            except Exception:
                return

    def _start_run_operation(
        self,
        work: RunWork,
        *,
        session_id: str,
        run_id: str,
        project: str,
        provider_id: str,
        turn_budget: int,
        max_repair_rounds: int,
        task_kind: str,
    ) -> None:
        if self.runtime_operations is None:
            return
        try:
            work.operation = self.runtime_operations.start(
                session_id=session_id,
                run_id=run_id,
                project=project,
                provider_id=provider_id,
                turn_budget=turn_budget,
                max_repair_rounds=max_repair_rounds,
                task_kind=task_kind,
            )
        except (OSError, ValueError, RuntimeOperationTransitionError):
            work.operation = None

    def _commit_run_operation(self, work: RunWork, transition: Callable) -> None:
        # Explanatory persistence is fail-open: one failed commit disables
        # this run's tracking instead of perturbing the coding run.
        if work.operation is None or self.runtime_operations is None:
            return
        try:
            work.operation = self.runtime_operations.commit(
                work.operation.session_id,
                work.operation.run_id,
                transition,
            )
        except (OSError, ValueError, RuntimeOperationTransitionError):
            work.operation = None

    def _finish_run_operation(self, work: RunWork, event: dict) -> None:
        # Same bounded fields RunLedger.finish persists; the terminal
        # snapshot and the ledger's run_finished row must agree.
        if work.operation is None:
            return
        max_turns = int(event.get("max_turns") or 0)
        self._commit_run_operation(
            work,
            lambda state: mark_terminal(
                state,
                stop_reason=str(event.get("stop_reason") or ""),
                summary_chars=len(str(event.get("summary") or "")),
                turns=terminal_turns(work, turns=event.get("turns"), max_turns=max_turns),
                max_turns=max_turns,
                provider=str(event.get("provider") or ""),
            ),
        )

    def _ghost_directive(
        self,
        *,
        project: str = "",
        session_id: str = "",
    ):
        store = getattr(self.state, "ghost_hebbian", None)
        if store is None:
            return build_ghost_directive(None)
        try:
            return build_ghost_directive(
                store,
                project=project,
                session_id=session_id,
                affinity_store=self._ghost_affinity_store(),
            )
        except Exception:
            return build_ghost_directive(None)

    def _ghost_directive_text(
        self,
        *,
        project: str = "",
        session_id: str = "",
    ) -> str:
        return self._ghost_directive(project=project, session_id=session_id).text

    def _ghost_affinity_store(self):
        store = getattr(self.state, "ghost_affinity", None)
        if store is None:
            return None
        inbox_store = getattr(self.state, "ghost_inbox", None)
        if inbox_store is not None:
            try:
                if not inbox_store.learning_enabled():
                    return None
            except Exception:
                return None
        return store

    def _ghost_continuity(
        self,
        *,
        project: str = "",
        session_id: str = "",
    ):
        store = getattr(self.state, "ghost_continuity", None)
        if store is None:
            return build_ghost_continuity(None)
        try:
            return build_ghost_continuity(
                store,
                project=project,
                session_id=session_id,
            )
        except Exception:
            return build_ghost_continuity(None)

    def _ghost_continuity_text(
        self,
        *,
        project: str = "",
        session_id: str = "",
    ) -> str:
        return self._ghost_continuity(project=project, session_id=session_id).text

    def _maybe_run_ghost_learning(
        self,
        frame: RunFrame | None,
        event: dict[str, object],
    ) -> None:
        if frame is None or self.ghost_learning_provider_factory is None:
            return
        mode = str(event.get("mode") or "")
        if mode not in self.ghost_learning_modes:
            return
        if str(event.get("stop_reason") or "") != "done":
            return
        try:
            loop = GhostLearningLoop(
                signal_store=getattr(self.state, "ghost_signals", None),
                inbox_store=getattr(self.state, "ghost_inbox", None),
                hebbian_store=getattr(self.state, "ghost_hebbian", None),
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
                provider_factory=self.ghost_learning_provider_factory,
                timeout=DEFAULT_GHOST_LEARNING_TIMEOUT,
                new_chat_timeout=DEFAULT_GHOST_LEARNING_NEW_CHAT_TIMEOUT,
            )
            self.state.emit(
                result.to_event(
                    run_id=frame.run_id,
                    session_id=frame.request.session_id,
                )
            )
        except Exception:
            return

    def _maybe_sync_ghost_continuity(
        self,
        frame: RunFrame | None,
        event: dict[str, object],
    ) -> None:
        if frame is None:
            return
        store = getattr(self.state, "ghost_continuity", None)
        if store is None:
            return
        mode = str(event.get("mode") or "")
        if mode not in {"chat", "planning"}:
            return
        inbox_store = getattr(self.state, "ghost_inbox", None)
        if inbox_store is not None and not inbox_store.learning_enabled():
            try:
                self.state.emit(
                    {
                        "type": "ghost_continuity_done",
                        "run_id": frame.run_id,
                        "session_id": frame.request.session_id,
                        "ok": True,
                        "skipped_reason": "learning_disabled",
                        "items_changed": 0,
                        "total_items": 0,
                        "warnings": [],
                    }
                )
            except Exception:
                pass
            return
        try:
            projection = (
                load_run_projection(self.run_ledgers, frame.request.session_id, frame.run_id)
                if self.run_ledgers is not None
                else None
            )
            result = store.sync_from_sources(
                hebbian_store=getattr(self.state, "ghost_hebbian", None),
                run_projection=projection,
                knowledge_store=self.knowledge_store,
                user_focus_excerpt=frame.request.task,
                session_id=frame.request.session_id,
                run_id=frame.run_id,
                project=frame.project_text if mode != "chat" else "",
                mode=mode,
            )
            self.state.emit(
                result.to_event(
                    run_id=frame.run_id,
                    session_id=frame.request.session_id,
                )
            )
        except Exception:
            return

    def _maybe_kick_ghost_sleep(
        self,
        frame: RunFrame | None,
        event: dict[str, object],
    ) -> None:
        if frame is None:
            return
        if str(event.get("stop_reason") or "") != "done":
            return
        kick = getattr(self.state, "kick_ghost_sleep", None)
        if not callable(kick):
            return
        try:
            projection = (
                load_run_projection(self.run_ledgers, frame.request.session_id, frame.run_id)
                if self.run_ledgers is not None
                else None
            )
            kick(
                trigger="post_turn",
                run_id=frame.run_id,
                session_id=frame.request.session_id,
                project=frame.project_text,
                run_projection=projection,
            )
        except Exception:
            return

    def _maybe_claim_ghost_work_item(
        self,
        request: TaskSubmission,
        *,
        run_id: str,
    ):
        if str(request.intent or "auto").strip().lower() != "auto":
            return None
        if not is_strict_work_continuation(request.task):
            return None
        store = getattr(self.state, "ghost_work_queue", None)
        if store is None:
            return None
        inbox_store = getattr(self.state, "ghost_inbox", None)
        if inbox_store is not None:
            try:
                if not inbox_store.learning_enabled():
                    return None
            except Exception:
                return None
        try:
            affinity_hints = ()
            affinity_store = self._ghost_affinity_store()
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

    def _maybe_sync_ghost_work_queue(
        self,
        frame: RunFrame | None,
        event: dict[str, object],
    ) -> None:
        if frame is None:
            return
        store = getattr(self.state, "ghost_work_queue", None)
        affinity_store = self._ghost_affinity_store()
        if store is None and affinity_store is None:
            return
        inbox_store = getattr(self.state, "ghost_inbox", None)
        if inbox_store is not None:
            try:
                if not inbox_store.learning_enabled():
                    return
            except Exception:
                return
        try:
            projection = (
                load_run_projection(self.run_ledgers, frame.request.session_id, frame.run_id)
                if self.run_ledgers is not None
                else None
            )
            research_interest_candidates = build_research_interest_candidates(
                self.knowledge_store,
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
                    continuity_store=getattr(self.state, "ghost_continuity", None),
                    work_checkpoint_store=self.work_checkpoints,
                    run_projection=projection,
                    terminal_event=event,
                    research_interest_candidates=research_interest_candidates,
                    session_id=frame.request.session_id,
                    run_id=frame.run_id,
                    project=frame.project_text,
                )
            if affinity_store is not None:
                affinity_store.sync_from_sources(
                    hebbian_store=getattr(self.state, "ghost_hebbian", None),
                    work_queue_store=store,
                    research_interest_candidates=research_interest_candidates,
                    router_store=getattr(self.state, "ghost_router", None),
                    run_projection=projection,
                    terminal_event=event,
                    session_id=frame.request.session_id,
                    project=frame.project_text,
                )
        except Exception:
            return

    def _maybe_sync_ghost_affinity_terminal_event(
        self,
        request: TaskSubmission,
        *,
        run_id: str,
        project_text: str,
        terminal_event: dict[str, object],
    ) -> None:
        affinity_store = self._ghost_affinity_store()
        if affinity_store is None:
            return
        try:
            projection = (
                load_run_projection(self.run_ledgers, request.session_id, run_id)
                if self.run_ledgers is not None
                else None
            )
            affinity_store.sync_from_sources(
                hebbian_store=getattr(self.state, "ghost_hebbian", None),
                work_queue_store=getattr(self.state, "ghost_work_queue", None),
                router_store=getattr(self.state, "ghost_router", None),
                run_projection=projection,
                terminal_event=terminal_event,
                session_id=request.session_id,
                project=project_text,
            )
        except Exception:
            return

    def _maybe_complete_ghost_work_item(
        self,
        frame: RunFrame | None,
        event: dict[str, object],
        item: GhostWorkItem | None,
        *,
        research_result: Any = None,
    ) -> None:
        if item is None:
            return
        store = getattr(self.state, "ghost_work_queue", None)
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
            projection = (
                load_run_projection(self.run_ledgers, frame.request.session_id, frame.run_id)
                if self.run_ledgers is not None
                else None
            )
            if str(event.get("stop_reason") or "") == "done":
                if str(getattr(item, "kind", "") or "") in RESEARCH_QUEUE_KINDS:
                    decision = ResearchCompletionGate(self.evidence_ledgers).evaluate(
                        item=item,
                        event=event,
                        research_result=research_result,
                        session_id=frame.request.session_id,
                        project=frame.project_text,
                    )
                    _record_completion_proof_trace(frame.trace, decision.proof)
                    if decision.complete:
                        store.complete_item(
                            item.id,
                            run_id=frame.run_id,
                            proof_refs=decision.proof_refs,
                        )
                    else:
                        _record_research_proof_review_trace(frame.trace, decision.review)
                        _record_research_plan_trace(
                            frame.trace,
                            decision.review,
                            question=_research_queue_item_title(item) or frame.request.task,
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

    def _maybe_release_ghost_work_item(
        self,
        item: GhostWorkItem | None,
        *,
        run_id: str,
        reason: str,
    ) -> None:
        if item is None:
            return
        store = getattr(self.state, "ghost_work_queue", None)
        if store is None:
            return
        try:
            store.release_item(item.id, run_id=run_id, reason=reason)
        except Exception:
            return

    def _maybe_route_auto(
        self,
        request: TaskSubmission,
        *,
        baseline_mode: str,
        run_id: str,
    ) -> GhostRouteResult | None:
        intent = str(request.intent or "auto").strip().lower()
        if intent != "auto":
            return None
        store = getattr(self.state, "ghost_router", None)
        if store is None:
            return None
        inbox_store = getattr(self.state, "ghost_inbox", None)
        if inbox_store is not None and not inbox_store.learning_enabled():
            return None
        provider_factory = self.ghost_router_provider_factory
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
            has_reviewable_diff=self._has_reviewable_diff(request.project),
        )
        try:
            with provider_controls.suppress_assistance():
                return GhostRouter(store).route(
                    route_request,
                    provider_factory=provider_factory,
                    timeout=min(DEFAULT_GHOST_ROUTER_TIMEOUT, PRODUCTION_GHOST_ROUTER_TIMEOUT),
                    new_chat_timeout=min(
                        DEFAULT_GHOST_ROUTER_NEW_CHAT_TIMEOUT,
                        PRODUCTION_GHOST_ROUTER_NEW_CHAT_TIMEOUT,
                    ),
                    max_attempts=PRODUCTION_GHOST_ROUTER_ATTEMPTS,
                )
        except (provider_controls.ControlTeachCancelled, cancellation.TaskCancelled):
            raise
        except Exception:
            return None

    def _has_reviewable_diff(self, project: str | None) -> bool:
        if not project:
            return False
        try:
            return has_reviewable_changes(self._collect_review_changes(project))
        except Exception:
            return False

    def _collect_review_changes(self, project: str | None) -> dict:
        if not project:
            return {"ok": False, "error": "project required", "files": [], "diff": ""}
        return self.collect_changes(project, self._review_change_tracker(project))

    def _review_change_tracker(self, project: str | None):
        if not project:
            return None
        try:
            key = str(Path(project).expanduser().resolve())
        except (OSError, RuntimeError, ValueError):
            return None
        tracker_for = getattr(self.state, "change_tracker_for", None)
        if not callable(tracker_for):
            return None
        try:
            persistent = not self.is_git_repository(key)
        except Exception:
            persistent = True
        try:
            return tracker_for(key, persistent=persistent)
        except Exception:
            return None

    def _event_with_projected_receipt(
        self,
        event: dict,
        *,
        session_id: str,
        run_id: str,
    ) -> dict:
        # The terminal event's receipt must be the receipt the ledger
        # durably recorded, not a parallel in-memory copy. If a run never
        # recorded final changes (research, review, chat, early errors),
        # keep whatever the mode already carried.
        if self.run_ledgers is None:
            return event
        projection = load_run_projection(self.run_ledgers, session_id, run_id)
        receipt = build_task_receipt_from_projection(projection)
        if receipt is None:
            return event
        updated = dict(event)
        updated["receipt"] = receipt.to_dict()
        return updated

    @staticmethod
    def _contract_from_submission(
        request: TaskSubmission,
        *,
        run_id: str,
        task_kind: str,
    ) -> TaskContract:
        return TaskContract(
            session_id=request.session_id,
            run_id=run_id,
            kind=task_kind,
            project=request.project,
            prompt=request.task,
            max_turns=request.max_turns,
            provider_id=request.provider_id,
            continue_task=request.continue_task,
            intent=request.intent,
        )

    def run(self, request: TaskSubmission) -> None:
        if self.runtime is None:
            self._run_impl(request)
            return
        self.runtime.run(request)

    def _run_impl(self, request: TaskSubmission) -> OperationOutcome | None:
        state = self.state
        session_id = request.session_id
        project = request.project
        task = request.task
        max_turns = request.max_turns
        continue_task = request.continue_task
        provider_id = request.provider_id
        baseline_task_kind = _resolve_task_kind(request)
        task_kind = baseline_task_kind
        run_id = request.run_id
        claimed_work_item: GhostWorkItem | None = None

        if not run_id:
            reserved = state.reserve_run(
                session_id=session_id,
                project=project,
                task=task,
                provider_id=provider_id,
            )
            if reserved is None:
                return None
            run_id = reserved.run_id
        if not state.start_run(run_id):
            return OperationOutcome.aborted(reason="run_not_started")

        contract = self._contract_from_submission(
            request,
            run_id=run_id,
            task_kind=baseline_task_kind,
        )
        task_kind = str(contract.kind)

        trace = None
        if self.run_traces is not None:
            try:
                trace = self.run_traces.open(
                    run_id=run_id,
                    session_id=session_id,
                    project=project,
                    mode_initial=_trace_mode(baseline_task_kind, project),
                    provider_initial=provider_id,
                )
            except Exception:
                trace = None
        trace_sink = FailOpenPromptTrace(trace)

        # One config read per run: failover ranking and the project context
        # builder share this load instead of each re-reading .codey/config.json.
        project_config_result = load_project_config(project) if project else ProjectConfigLoadResult()

        def finish_trace(event: dict[str, object]) -> None:
            status = str(event.get("stop_reason") or "done")
            trace_sink.call(
                "finish",
                status=status,
                mode=_trace_mode(task_kind, project),
                provider=str(event.get("provider") or current_provider_id()),
            )

        provider_controls.set_teach_handler(state.handle_control_teach)
        provider_controls.set_doctor_handler(getattr(state, "handle_profile_doctor", None))
        provider_flow.set_recovery_handler(getattr(state, "handle_flow_recovery", None))
        provider_controls.begin_task_context(session_id)
        state.last_provider_failure = None
        previous_cancel_event = cancellation.set_event(state.stop_flag)
        route_result = None
        try:
            claim_result = self._maybe_claim_ghost_work_item(request, run_id=run_id)
            if claim_result is not None:
                claimed_work_item = claim_result.item
                task_kind = claim_result.mode or task_kind
                request = replace(
                    request,
                    task=claim_result.task or request.task,
                    continue_task=True,
                )
                task = request.task
                continue_task = request.continue_task
            else:
                route_result = self._maybe_route_auto(
                    request,
                    baseline_mode=baseline_task_kind,
                    run_id=run_id,
                )
                if route_result is not None:
                    task_kind = route_result.final_mode
        except (provider_controls.ControlTeachCancelled, cancellation.TaskCancelled):
            state.set_provider_session(provider_id, None)
            self._maybe_release_ghost_work_item(
                claimed_work_item,
                run_id=run_id,
                reason="stopped_before_start",
            )
            stopped_event = task_done_event(
                run_id=run_id,
                session_id=session_id,
                summary="",
                stop_reason="stopped",
                max_turns=max_turns,
                provider=provider_id,
                mode=_ui_mode(baseline_task_kind, project),
            )
            trace_sink.call(
                "record_router",
                baseline_mode=_trace_mode(baseline_task_kind, project),
                selected_mode=_trace_mode(task_kind, project),
                final_mode=_trace_mode(task_kind, project),
                source="local_work_item" if claimed_work_item else "baseline",
                reason_code="stopped_before_start",
            )
            finish_trace(stopped_event)
            state.finish_run(run_id, stopped_event)
            cancellation.set_event(previous_cancel_event)
            provider_controls.end_task_context()
            return operation_outcome_from_task_done_event(stopped_event)
        except BaseException as exc:
            # Any other failure inside the claim/route window must still
            # restore the previous cancellation event and task context, or
            # later runs on this thread inherit our stop flag. The run slot
            # is already started, so it also gets a bounded error terminal
            # event: an exception here must not leave the runner busy
            # forever.
            state.set_provider_session(provider_id, None)
            self._maybe_release_ghost_work_item(
                claimed_work_item,
                run_id=run_id,
                reason="aborted_before_start",
            )
            error_event = task_done_event(
                run_id=run_id,
                session_id=session_id,
                summary=f"ERROR: {exc}",
                stop_reason="error",
                max_turns=max_turns,
                provider=provider_id,
                mode=_ui_mode(task_kind, project),
            )
            finish_trace(error_event)
            state.finish_run(run_id, error_event)
            cancellation.set_event(previous_cancel_event)
            provider_controls.end_task_context()
            raise
        route_source = "explicit_user_choice" if str(request.intent or "").strip().lower() != "auto" else "baseline"
        route_reason = "intent_selected" if route_source == "explicit_user_choice" else "baseline_kept"
        route_selected_mode = task_kind
        if claimed_work_item is not None:
            route_source = "local_work_item"
            route_reason = "claimed_work_item"
        elif route_result is not None:
            route_source = "auto_router"
            route_selected_mode = route_result.selected_mode or route_result.final_mode or task_kind
            route_reason = "accepted" if route_result.accepted else (route_result.skipped_reason or "baseline_kept")
        trace_sink.call(
            "record_router",
            baseline_mode=_trace_mode(baseline_task_kind, project),
            selected_mode=_trace_mode(route_selected_mode, project),
            final_mode=_trace_mode(task_kind, project),
            source=route_source,
            reason_code=route_reason,
            overridden_by_user=(route_source == "explicit_user_choice"),
        )
        state.emit(
            {
                "type": "task_start",
                "run_id": run_id,
                "session_id": session_id,
                "project": project,
                "task": task,
                "mode": _ui_mode(task_kind, project),
                "max_turns": max_turns,
                "continue_task": continue_task,
                "provider": provider_id,
                "intent": request.intent,
            }
        )

        work = RunWork(
            recent_events=[],
            evidence=ExecutionEvidence(),
            claimed_work_item=claimed_work_item,
            trace=trace,
        )
        project_completion_deps = ProjectCompletionDeps(
            state=state,
            agent_run=self.agent_run,
            collect_changes=self.collect_changes,
            run_review=self.run_review,
            capture_provider_failure=self.capture_provider_failure,
            commit_run_operation=self._commit_run_operation,
            run_consensus=self.run_consensus,
            run_project_audit=self.run_project_audit,
            project_facts=self.project_facts,
            work_checkpoints=self.work_checkpoints,
            managed_outputs=self.managed_outputs,
            knowledge_store=self.knowledge_store,
            is_git_repository=self.is_git_repository,
            review_fix_turns=self.review_fix_turns,
            review_log_lines=self.review_log_lines,
        )
        self._start_run_operation(
            work,
            session_id=session_id,
            run_id=run_id,
            project=project or "",
            provider_id=provider_id,
            turn_budget=max_turns,
            max_repair_rounds=MAX_COMPLETION_REPAIR_ROUNDS,
            task_kind=task_kind,
        )
        if (
            project
            and task_kind in {"project", "hybrid", "planning_readonly", "review"}
            and self.run_ledgers is not None
        ):
            try:
                work.ledger = self.run_ledgers.open(
                    run_id=run_id,
                    session_id=session_id,
                    project=project,
                    task=task,
                    provider=provider_id,
                    mode=_ui_mode(task_kind, project),
                )
                work.record_agent_events_in_ledger = task_kind in {"project", "planning_readonly"}
            except Exception:
                work.ledger = None
        frame: RunFrame | None = None
        provider: Any | None = None
        logged_provider_failures: set[tuple[str, str, str, str]] = set()

        def append_ledger(action: Callable[[RunLedgerWriter], None]) -> None:
            if work.ledger is None:
                return
            try:
                action(work.ledger)
            except Exception:
                work.ledger = None

        def append_ledger_provider_failure(pid: str, failure: ProviderFailure) -> None:
            key = (
                str(pid),
                str(getattr(failure, "action", "")),
                str(getattr(failure, "kind", "")),
                str(getattr(failure, "message", "")),
            )
            if key in logged_provider_failures:
                return
            logged_provider_failures.add(key)
            append_ledger(lambda ledger: ledger.append_provider_failure(pid, failure))

        def current_provider_id() -> str:
            return frame.provider_id if frame is not None else provider_id

        def current_provider() -> Any | None:
            return frame.provider if frame is not None else provider

        def update_checkpoint(
            action: Callable[[WorkCheckpointStore, WorkCheckpoint], WorkCheckpoint],
        ) -> None:
            if self.work_checkpoints is None or work.work_checkpoint is None:
                return
            try:
                work.work_checkpoint = action(
                    self.work_checkpoints,
                    work.work_checkpoint,
                )
            except (OSError, ValueError):
                pass

        def on_event(event: RunEvent) -> None:
            work.turns_observed = max(
                work.turns_observed,
                nonnegative_event_count(event.turn),
            )
            if work.record_agent_events_in_ledger:
                append_ledger(lambda ledger: ledger.append_run_event(event))
            payload = run_event_ui_payload(run_id, session_id, event)
            if payload is not None:
                state.emit(payload)
            if event.kind == "tool_start":
                return
            work.evidence.record(event)
            message = render_run_event(event)
            work.recent_events.append(message)
            if len(work.recent_events) > self.review_log_lines * 2:
                del work.recent_events[: self.review_log_lines]
            if project and event.kind == "tool" and event.call is not None and event.outcome is not None:
                handle_project_tool_event(
                    project_completion_deps,
                    event=event,
                    project=project,
                    work=work,
                    run_id=run_id,
                    update_checkpoint=update_checkpoint,
                )

        def on_shell_request(cwd_rel: str, command: str) -> None:
            if not project:
                return
            risk = classify_shell_risk(command)
            approval_id = "shell_" + uuid.uuid4().hex[:12]
            pending = {
                "id": approval_id,
                "session_id": session_id,
                "project": project,
                "cwd": cwd_rel or ".",
                "command": command,
                "risk_label": risk.label,
                "risk_title": risk.title,
                "risk_detail": risk.detail,
                "post_approval_instructions": risk.post_approval_instructions,
                "max_turns": max_turns,
                "provider": current_provider_id(),
                "continue_after": True,
                "run_id": run_id,
            }
            pending["ui_event"] = {
                "type": "shell_request",
                "run_id": run_id,
                "session_id": session_id,
                "id": approval_id,
                "project": project,
                "cwd": pending["cwd"],
                "command": command,
                "risk_label": risk.label,
                "risk_title": risk.title,
                "risk_detail": risk.detail,
            }
            state.add_pending_shell_approval(approval_id, pending)
            state.emit(pending["ui_event"])

        try:
            supervisor = getattr(state, "provider_supervisor", None)
            self_repair = getattr(state, "self_repair", None)

            def record_provider_failure(pid: str, failure: ProviderFailure) -> None:
                append_ledger_provider_failure(pid, failure)
                trace_sink.call("record_provider_failure", pid, failure)
                if supervisor is None:
                    return
                health = supervisor.record_failure(pid, failure)
                if self_repair is not None:
                    try:
                        self_repair.maybe_enqueue(pid, failure, health)
                    except Exception:
                        pass

            def provider_failover_order() -> tuple[str, ...]:
                loader = getattr(state, "provider_failover_order", None)
                try:
                    return tuple(loader()) if loader is not None else tuple(PROVIDER_LABELS)
                except Exception:
                    return tuple(PROVIDER_LABELS)

            def ranked_failover_order() -> tuple[str, ...]:
                mode = _startup_failover_mode(task_kind)
                return rank_providers(
                    provider_failover_order(),
                    mode=mode,
                    # Soft preference only: project config re-ranks the
                    # candidates; it cannot override the user's explicit
                    # provider or supervisor availability decisions.
                    preferred=preferred_provider_for(project_config_result.config, mode),
                )

            if task_kind == "review":
                project_text = str(Path(project).expanduser().resolve()) if project else ""
                conversation = state.conversation_for(session_id)
                frame = RunFrame(
                    request=request,
                    run_id=run_id,
                    task_kind=task_kind,
                    provider=None,
                    provider_id=provider_id,
                    project_text=project_text,
                    conversation=conversation,
                    fresh_chat=False,
                    handoff="",
                    research_handoff="",
                    prior_snapshot=conversation.snapshot,
                    recovered_owner_prompt="",
                    provider_session_changed=False,
                    preflight_tried=set(),
                    preflight_switches=0,
                    trace=trace,
                )
                outcome = self._run_review_mode(frame)
                append_ledger(lambda ledger: ledger.finish(**outcome.event))
                event = self._event_with_projected_receipt(
                    outcome.event,
                    session_id=session_id,
                    run_id=run_id,
                )
                self._finish_run_operation(work, event)
                finish_trace(event)
                state.finish_run(run_id, event)
                self._maybe_complete_ghost_work_item(
                    frame,
                    event,
                    work.claimed_work_item,
                    research_result=outcome.research_result,
                )
                self._maybe_run_ghost_learning(frame, event)
                self._maybe_sync_ghost_continuity(frame, event)
                self._maybe_sync_ghost_work_queue(frame, event)
                self._maybe_kick_ghost_sleep(frame, event)
                return operation_outcome_from_task_done_event(event)

            preflight = connect_provider_with_preflight(
                state=state,
                run_id=run_id,
                provider_id=provider_id,
                supervisor=supervisor,
                ranked_failover_order=ranked_failover_order,
                capture_provider_failure=self.capture_provider_failure,
                record_provider_failure=record_provider_failure,
                append_ledger=append_ledger,
                trace_sink=trace_sink,
            )
            provider = preflight.provider
            provider_id = preflight.provider_id
            preflight_tried = preflight.tried
            preflight_switches = preflight.switches
            conversation = state.conversation_for(session_id)
            conversation_plan = build_conversation_plan(
                state=state,
                session_id=session_id,
                provider_id=provider_id,
                provider=provider,
                conversation=conversation,
                task_kind=task_kind,
                project=project,
                task=task,
                continue_task=continue_task,
                trace=trace,
            )
            project_text = str(Path(project).expanduser().resolve()) if project else ""
            frame = RunFrame(
                request=request,
                run_id=run_id,
                task_kind=task_kind,
                provider=provider,
                provider_id=provider_id,
                project_text=project_text,
                conversation=conversation_plan.conversation,
                fresh_chat=conversation_plan.fresh_chat,
                handoff=conversation_plan.handoff,
                research_handoff=conversation_plan.research_handoff,
                prior_snapshot=conversation_plan.prior_snapshot,
                recovered_owner_prompt=conversation_plan.recovered_owner_prompt,
                provider_session_changed=conversation_plan.provider_session_changed,
                preflight_tried=preflight_tried,
                preflight_switches=preflight_switches,
                trace=trace,
            )
            hooks = RunHooks(
                on_event=on_event,
                on_shell_request=on_shell_request,
                update_checkpoint=update_checkpoint,
                record_provider_failure=record_provider_failure,
                append_ledger=append_ledger,
                provider_failover_order=provider_failover_order,
                supervisor=supervisor,
                trace=trace,
            )

            def run_project_operation(
                active_frame: RunFrame,
                active_work: RunWork,
                active_hooks: RunHooks,
                **kwargs,
            ) -> ModeOutcome:
                return run_project_mode(
                    project_completion_deps,
                    active_frame,
                    active_work,
                    active_hooks,
                    **kwargs,
                )

            if task_kind == "research":
                outcome = run_research_mode(
                    self._research_deps(),
                    frame,
                    hooks,
                    proof_question=_research_queue_item_title(work.claimed_work_item),
                    run_pipeline=lambda active_frame, active_hooks, **kwargs: run_research_pipeline(
                        self._research_deps(),
                        active_frame,
                        active_hooks,
                        record_ledger_write=record_evidence_ledger_write,
                        **kwargs,
                    ),
                )
            elif task_kind == "hybrid":
                outcome = run_hybrid_mode(
                    self._research_deps(),
                    frame,
                    work,
                    hooks,
                    config_result=project_config_result,
                    run_project=run_project_operation,
                    run_pipeline=lambda active_frame, active_hooks, **kwargs: run_research_pipeline(
                        self._research_deps(),
                        active_frame,
                        active_hooks,
                        record_ledger_write=record_evidence_ledger_write,
                        **kwargs,
                    ),
                )
            elif task_kind == "planning_readonly":
                outcome = self._run_planning_readonly_mode(
                    frame,
                    work,
                    config_result=project_config_result,
                )
            elif task_kind == "project":
                outcome = run_project_operation(
                    frame,
                    work,
                    hooks,
                    config_result=project_config_result,
                )
            else:
                outcome = self._run_chat_mode(frame)
            append_ledger(lambda ledger: ledger.finish(**outcome.event))
            event = self._event_with_projected_receipt(
                outcome.event,
                session_id=session_id,
                run_id=run_id,
            )
            self._finish_run_operation(work, event)
            finish_trace(event)
            state.finish_run(run_id, event)
            self._maybe_complete_ghost_work_item(
                frame,
                event,
                work.claimed_work_item,
                research_result=outcome.research_result,
            )
            self._maybe_run_ghost_learning(frame, event)
            self._maybe_sync_ghost_continuity(frame, event)
            self._maybe_sync_ghost_work_queue(frame, event)
            self._maybe_kick_ghost_sleep(frame, event)
            return operation_outcome_from_task_done_event(event)
        except (provider_controls.ControlTeachCancelled, cancellation.TaskCancelled):
            current_id = current_provider_id()
            state.set_provider_session(current_id, None)
            update_checkpoint(lambda store, item: store.set_status(item, "interrupted", "stopped"))
            current_conversation = (
                frame.conversation if frame is not None else (conversation if "conversation" in locals() else None)
            )
            if current_conversation is not None:
                current_conversation.update_snapshot(
                    replace(
                        current_conversation.snapshot,
                        provider_id=current_id,
                        blocker="stopped",
                    )
                )
            stopped_event = task_done_event(
                run_id=run_id,
                session_id=session_id,
                summary="",
                stop_reason="stopped",
                max_turns=max_turns,
                provider=current_id,
                mode=_ui_mode(task_kind, project),
                work=work,
            )
            append_ledger(lambda ledger: ledger.finish(**stopped_event))
            stopped_event = self._event_with_projected_receipt(
                stopped_event,
                session_id=session_id,
                run_id=run_id,
            )
            self._finish_run_operation(work, stopped_event)
            finish_trace(stopped_event)
            state.finish_run(run_id, stopped_event)
            self._maybe_release_ghost_work_item(
                work.claimed_work_item if "work" in locals() else claimed_work_item,
                run_id=run_id,
                reason="stopped",
            )
            return operation_outcome_from_task_done_event(stopped_event)
        except Exception as exc:
            current_id = current_provider_id()
            current_item = current_provider()
            update_checkpoint(lambda store, item: store.set_status(item, "interrupted", "error"))
            current_conversation = (
                frame.conversation if frame is not None else (conversation if "conversation" in locals() else None)
            )
            if current_conversation is not None:
                current_conversation.update_snapshot(
                    replace(
                        current_conversation.snapshot,
                        provider_id=current_id,
                        blocker=str(exc),
                    )
                )
            failure = (
                exc.failure
                if isinstance(exc, ProviderActionError)
                else self.capture_provider_failure(
                    model=PROVIDER_LABELS.get(current_id, current_id),
                    action="task" if current_item is not None else "connect",
                    page=None,
                    error=exc,
                )
            )
            state.last_provider_failure = failure
            if failure is not None:
                append_ledger_provider_failure(current_id, failure)
            error_event = task_done_event(
                run_id=run_id,
                session_id=session_id,
                summary=f"ERROR: {exc}",
                stop_reason="error",
                max_turns=max_turns,
                provider=current_id,
                mode=_ui_mode(task_kind, project),
                work=work,
                provider_failure=failure.to_dict() if failure else None,
            )
            append_ledger(lambda ledger: ledger.finish(**error_event))
            error_event = self._event_with_projected_receipt(
                error_event,
                session_id=session_id,
                run_id=run_id,
            )
            self._finish_run_operation(work, error_event)
            finish_trace(error_event)
            state.finish_run(run_id, error_event)
            self._maybe_complete_ghost_work_item(
                frame,
                error_event,
                work.claimed_work_item if "work" in locals() else claimed_work_item,
            )
            if frame is not None:
                self._maybe_sync_ghost_work_queue(frame, error_event)
            else:
                self._maybe_sync_ghost_affinity_terminal_event(
                    request,
                    run_id=run_id,
                    project_text=str(project or ""),
                    terminal_event=error_event,
                )
            return operation_outcome_from_task_done_event(error_event)
        finally:
            cancellation.set_event(previous_cancel_event)
            provider_controls.end_task_context()
            try:
                current_item = current_provider()
                if current_item is not None:
                    current_item.close()
            except Exception:
                pass

    def _run_chat_mode(self, frame: RunFrame) -> ModeOutcome:
        return run_chat_mode(
            frame,
            state=self.state,
            run_consensus=self.run_consensus,
            ghost_directive=self._ghost_directive,
            ghost_continuity=self._ghost_continuity,
        )

    def _run_planning_readonly_mode(
        self,
        frame: RunFrame,
        work: RunWork,
        *,
        config_result: ProjectConfigLoadResult | None = None,
    ) -> ModeOutcome:
        state = self.state
        request = frame.request
        project = request.project
        if project is None:
            raise RuntimeError("planning_readonly mode requires a project")
        if frame.provider is None:
            raise RuntimeError("provider is not connected")
        if work.ledger is not None:
            work.record_agent_events_in_ledger = True
        context_builder = ProjectTaskContextBuilder(
            project_facts=self.project_facts,
            work_checkpoints=None,
            knowledge_store=self.knowledge_store,
            config_result=config_result,
        )
        project_context = context_builder.build(
            project=project,
            task=request.task,
            session_id=request.session_id,
            run_id=frame.run_id,
            continue_task=False,
            provider_session_changed=frame.provider_session_changed,
        )
        ghost_directive = self._ghost_directive(
            project=project,
            session_id=request.session_id,
        )
        ghost_continuity = self._ghost_continuity(
            project=project,
            session_id=request.session_id,
        )
        _record_local_context_trace(frame.trace, ghost_directive, ghost_continuity)
        result = self.agent_run(
            frame.provider,
            Path(project),
            request.task,
            max_turns=request.max_turns,
            on_event=lambda event: self._planning_event(frame, work, event),
            on_shell_request=None,
            stop_flag=state.stop_flag,
            fresh_chat=frame.fresh_chat,
            strict_fresh_chat=False,
            change_tracker=None,
            conversation=frame.conversation,
            provider_id=frame.provider_id,
            handoff=frame.handoff,
            project_facts=project_context.verified_facts,
            research_context=project_context.research_context,
            project_map=project_context.project_map,
            project_config_warnings=project_context.project_config_warnings,
            ghost_directive=ghost_directive.text,
            ghost_continuity=ghost_continuity.text,
            permission_profile="planning_readonly",
            trace_recorder=frame.trace,
        )
        state.set_provider_session(
            frame.provider_id,
            None if result.stop_reason == "stopped" else request.session_id,
        )
        frame.conversation.update_snapshot(
            replace(
                frame.conversation.snapshot,
                provider_id=frame.provider_id,
                checks_passed=False,
                summary=result.summary,
                blocker="" if result.stop_reason == "done" else result.summary,
            )
        )
        return ModeOutcome(
            {
                "type": "task_done",
                "run_id": frame.run_id,
                "session_id": request.session_id,
                "summary": result.summary,
                "stop_reason": result.stop_reason,
                "turns": result.turns,
                "max_turns": request.max_turns,
                "provider": frame.provider_id,
                "mode": "planning",
                "changed": False,
            }
        )

    def _run_review_mode(self, frame: RunFrame) -> ModeOutcome:
        state = self.state
        request = frame.request
        project = request.project
        trace = FailOpenPromptTrace(frame.trace)
        trace.call("record_permission_profile", "reviewer", phase="review")
        trace.record_section(
            PromptEnvelopeSection(
                name="review_request",
                text=request.task,
                purpose="review request from the user",
                freshness="run_start",
                source_refs=("request:review",),
            )
        )
        if project is None:
            summary = "No attached project is available to review."
            state.emit(
                {
                    "type": "review",
                    "run_id": frame.run_id,
                    "session_id": request.session_id,
                    "text": summary,
                }
            )
            return ModeOutcome(
                task_done_event(
                    run_id=frame.run_id,
                    session_id=request.session_id,
                    summary=summary,
                    stop_reason="done",
                    turns=0,
                    max_turns=request.max_turns,
                    provider=frame.provider_id,
                    mode="review",
                    changed=False,
                )
            )
        changes = self._collect_review_changes(project)
        trace.record_section(
            PromptEnvelopeSection(
                name="review_changes",
                text=changes.get("diff", "") if isinstance(changes, dict) else "",
                purpose="bounded local diff prepared for review",
                freshness="run_start",
                source_refs=("local_diff:review",),
            )
        )
        if not isinstance(changes, dict) or changes.get("ok") is not True:
            summary = "Could not collect a local diff to review."
            state.emit(
                {
                    "type": "review",
                    "run_id": frame.run_id,
                    "session_id": request.session_id,
                    "text": summary,
                }
            )
            return ModeOutcome(
                task_done_event(
                    run_id=frame.run_id,
                    session_id=request.session_id,
                    summary=summary,
                    stop_reason="done",
                    turns=0,
                    max_turns=request.max_turns,
                    provider=frame.provider_id,
                    mode="review",
                    changed=False,
                )
            )
        if not has_reviewable_changes(changes):
            summary = "No reviewable local diff was found."
            state.emit(
                {
                    "type": "review",
                    "run_id": frame.run_id,
                    "session_id": request.session_id,
                    "text": summary,
                }
            )
            return ModeOutcome(
                task_done_event(
                    run_id=frame.run_id,
                    session_id=request.session_id,
                    summary=summary,
                    stop_reason="done",
                    turns=0,
                    max_turns=request.max_turns,
                    provider=frame.provider_id,
                    mode="review",
                    changed=False,
                    changes={
                        "changed_count": changes.get("changed_count", 0),
                        "files": changes.get("files", [])[:3],
                        "mode": changes.get("mode"),
                        "project": project,
                    },
                )
            )
        try:
            try:
                review_impact_map = safe_review_impact_map(project, changes)
            except cancellation.TaskCancelled:
                raise
            except Exception:
                review_impact_map = ""
            record_review_input_prepared_trace(
                frame.trace,
                task=request.task,
                writer_summary="Review-only mode did not run a writer.",
                changes=changes,
                recent_log="",
                change_brief="",
                project_map="",
                verification_map="",
                review_impact_map=review_impact_map,
                execution_evidence="",
            )
            reviewed = self.run_review(
                session_id=request.session_id,
                project=project,
                task=request.task,
                writer_summary="Review-only mode did not run a writer.",
                changes=changes,
                recent_log="",
                writer_id=frame.provider_id,
                change_brief="",
                project_map="",
                verification_map="",
                review_impact_map=review_impact_map,
                execution_evidence="",
                trace_recorder=frame.trace,
            )
        except cancellation.TaskCancelled:
            raise
        except Exception:
            reviewed = None
        if reviewed is None:
            summary = "Review unavailable. No files were changed."
        else:
            _reviewer_id, review = reviewed
            summary = _render_review_only_summary(review)
        state.set_provider_session(frame.provider_id, None)
        frame.conversation.update_snapshot(
            replace(
                frame.conversation.snapshot,
                mode="review",
                goal=request.task,
                project=frame.project_text,
                provider_id=frame.provider_id,
                changed_files=changed_paths_from_changes(changes),
                checks_passed=False,
                summary=summary,
                blocker="",
                latest_user=request.task,
                latest_reply=summary,
            )
        )
        state.emit(
            {
                "type": "review",
                "run_id": frame.run_id,
                "session_id": request.session_id,
                "text": summary,
            }
        )
        return ModeOutcome(
            {
                "type": "task_done",
                "run_id": frame.run_id,
                "session_id": request.session_id,
                "summary": summary,
                "stop_reason": "done",
                "turns": 1 if reviewed is not None else 0,
                "max_turns": request.max_turns,
                "provider": frame.provider_id,
                "mode": "review",
                "changed": False,
                "changes": {
                    "changed_count": changes.get("changed_count", 0),
                    "files": changes.get("files", [])[:3],
                    "mode": changes.get("mode"),
                    "project": project,
                },
            }
        )

    def _planning_event(
        self,
        frame: RunFrame,
        work: RunWork,
        event: RunEvent,
    ) -> None:
        if work.record_agent_events_in_ledger and work.ledger is not None:
            try:
                work.ledger.append_run_event(event)
            except Exception:
                work.ledger = None
        payload = run_event_ui_payload(frame.run_id, frame.request.session_id, event)
        if payload is not None:
            self.state.emit(payload)
        if event.kind == "tool_start":
            return
        work.evidence.record(event)
        message = render_run_event(event)
        work.recent_events.append(message)
        if len(work.recent_events) > self.review_log_lines * 2:
            del work.recent_events[: self.review_log_lines]
