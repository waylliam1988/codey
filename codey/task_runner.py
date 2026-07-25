"""Task orchestration independent of HTTP request handling."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from codey import cancellation, provider_controls, provider_flow
from codey.agent import RunResult
from codey.change_brief import (
    ChangeBrief,
    new_project_change_brief,
    project_audit_change_brief,
)
from codey.consensus import (
    render_project_context,
)
from codey.events import RunEvent, render_run_event
from codey.execution_evidence import ExecutionEvidence
from codey.handoff import (
    ConversationContext,
    ConversationSnapshot,
    render_continuation_prompt,
    render_handoff,
    render_recovered_handoff,
)
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.knowledge.brief import KnowledgeBriefBuilder
from codey.project_facts import ProjectFactsStore
from codey.project_task_context import (
    ProjectTaskContextBuilder,
    safe_project_map,
    safe_verification_candidates,
)
from codey.providers import PROVIDER_LABELS
from codey.provider_diagnostics import ProviderActionError, ProviderFailure
from codey.provider_supervisor import run_half_open_canary
from codey.receipt import build_task_receipt
from codey.research.browser_search import BrowserSearchProvider
from codey.research.runner import ResearchRunner
from codey.review_coordinator import ReviewCoordinator, change_state
from codey.shell_risk import classify_shell_risk
from codey.verification_map import render_verification_map
from codey.verification_policy import (
    check_covers_selected_candidate,
    select_verification_candidate,
    selected_verification_candidate_lines,
    verification_candidate_lines,
)
from codey.work_checkpoint import (
    WorkCheckpoint,
    WorkCheckpointStore,
)
from codey.writer_failover import (
    CheckpointView,
    WriterAttempt,
    WriterFailoverRunner,
)


@dataclass(frozen=True)
class TaskRequest:
    session_id: str
    project: str | None
    task: str
    max_turns: int
    continue_task: bool
    provider_id: str
    intent: str = "auto"
    run_id: str = ""


@dataclass
class _RunFrame:
    request: TaskRequest
    run_id: str
    task_kind: str
    provider: Any | None
    provider_id: str
    project_text: str
    conversation: ConversationContext
    fresh_chat: bool
    handoff: str
    research_handoff: str
    prior_snapshot: ConversationSnapshot
    recovered_owner_prompt: str
    provider_session_changed: bool
    preflight_tried: set[str]
    preflight_switches: int


@dataclass
class _RunWork:
    recent_events: list[str]
    evidence: ExecutionEvidence
    work_checkpoint: WorkCheckpoint | None = None


@dataclass(frozen=True)
class _RunHooks:
    on_event: Callable[[RunEvent], None]
    on_shell_request: Callable[[str, str], None]
    update_checkpoint: Callable[
        [Callable[[WorkCheckpointStore, WorkCheckpoint], WorkCheckpoint]],
        None,
    ]
    record_provider_failure: Callable[[str, ProviderFailure], None]
    provider_failover_order: Callable[[], tuple[str, ...]]
    supervisor: Any | None


@dataclass(frozen=True)
class _ModeOutcome:
    event: dict


NEW_PROJECT_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".codey",
    ".idea",
    ".vscode",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".next",
    "dist",
    "build",
}
NEW_PROJECT_IGNORED_FILES = {
    ".DS_Store",
    "Thumbs.db",
    ".gitignore",
    ".gitattributes",
    ".gitkeep",
}


def _project_has_user_files(project: str | Path) -> bool:
    """Return true when a project has real user files worth inspecting first."""

    stack = [Path(project).expanduser()]
    while stack:
        current = stack.pop()
        try:
            entries = tuple(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name in NEW_PROJECT_IGNORED_DIRS:
                continue
            try:
                if entry.is_dir() and not entry.is_symlink():
                    stack.append(entry)
                elif entry.is_file() and name not in NEW_PROJECT_IGNORED_FILES:
                    return True
            except OSError:
                continue
    return False


def _safe_verification_map(
    project: str | Path,
    changes: dict,
    checks: tuple[object, ...],
    project_map: str,
    recommended_commands: tuple[str, ...] = (),
) -> str:
    try:
        return render_verification_map(
            project,
            changes,
            checks_after_last_change=checks,
            project_map=project_map,
            recommended_commands=recommended_commands,
        )
    except Exception:
        return ""


def _changed_file_paths(changes: object) -> tuple[str, ...]:
    if not isinstance(changes, dict):
        return ()
    files = changes.get("files")
    if not isinstance(files, list):
        return ()
    return tuple(str(item.get("path") or "") for item in files if isinstance(item, dict) and item.get("path"))


def _resolve_task_kind(request: TaskRequest) -> str:
    intent = (request.intent or "auto").strip().lower()
    if intent in {"research", "project", "hybrid", "chat"}:
        if intent == "hybrid" and not request.project:
            return "research"
        if intent == "project" and not request.project:
            return "chat"
        return intent
    return "project" if request.project else "chat"


def _ui_mode(kind: str, project: str | None) -> str:
    if kind == "research":
        return "research"
    if kind == "hybrid":
        return "hybrid"
    return "agent" if project else "chat"


def _bullet_lines(values: tuple[str, ...]) -> str:
    if not values:
        return "- (none)"
    return "\n".join(f"- {item}" for item in values)


def _display_tool(name: str, args: dict, path: str = "") -> tuple[str, str]:
    research_names = {
        "web_search": ("search", str(args.get("query") or "")),
        "open_url": ("read", str(args.get("url") or "")),
        "knowledge_search": ("recall", str(args.get("query") or "")),
        "knowledge_read": ("note", str(args.get("id") or args.get("note_id") or "")),
        "knowledge_write": ("note", str(args.get("title") or args.get("type") or "")),
        "knowledge_link": ("link", str(args.get("src") or "")),
    }
    if name in research_names:
        kind, label = research_names[name]
        return kind, label[:160]
    return name, "" if path == "." else path


def _research_payload(result) -> dict:
    return {
        "synthesis_id": result.synthesis_id,
        "notes_created": result.notes_created,
        "notes_updated": result.notes_updated,
        "sources_read": result.sources_read,
        "source_urls": result.source_urls,
        "queries": result.queries,
        "search_results": result.search_results,
        "opened_sources": result.opened_sources,
        "coverage": result.coverage,
        "citation_map": result.citation_map,
        "evidence_items": result.evidence_items,
        "counterpoints": result.counterpoints,
        "quality_warnings": result.quality_warnings,
    }


class TaskRunner:
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
        knowledge_store: KnowledgeStore | None = None,
        search_factory: Callable[[], object] | None = None,
        is_git_repository: Callable[[str | Path], bool] | None = None,
        review_fix_turns: int = 12,
        review_log_lines: int = 80,
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
        self.knowledge_store = knowledge_store
        self.search_factory = search_factory or BrowserSearchProvider
        self.is_git_repository = is_git_repository or (lambda _project: False)
        self.review_fix_turns = review_fix_turns
        self.review_log_lines = review_log_lines

    def run(self, request: TaskRequest) -> None:
        state = self.state
        session_id = request.session_id
        project = request.project
        task = request.task
        max_turns = request.max_turns
        continue_task = request.continue_task
        provider_id = request.provider_id
        task_kind = _resolve_task_kind(request)
        run_id = request.run_id

        if not run_id:
            reserved = state.reserve_run(
                session_id=session_id,
                project=project,
                task=task,
                provider_id=provider_id,
            )
            if reserved is None:
                return
            run_id = reserved.run_id
        if not state.start_run(run_id):
            return

        provider_controls.set_teach_handler(state.handle_control_teach)
        provider_controls.set_doctor_handler(getattr(state, "handle_profile_doctor", None))
        provider_flow.set_recovery_handler(
            getattr(state, "handle_flow_recovery", None)
        )
        provider_controls.begin_task_context(session_id)
        state.last_provider_failure = None
        previous_cancel_event = cancellation.set_event(state.stop_flag)
        state.emit({
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
        })

        work = _RunWork(recent_events=[], evidence=ExecutionEvidence())
        frame: _RunFrame | None = None
        provider: Any | None = None

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
            payload = self._ui_event(run_id, session_id, event)
            if payload is not None:
                state.emit(payload)
            if event.kind == "tool_start":
                return
            work.evidence.record(event)
            message = render_run_event(event)
            work.recent_events.append(message)
            if len(work.recent_events) > self.review_log_lines * 2:
                del work.recent_events[:self.review_log_lines]
            if (
                project
                and self.project_facts is not None
                and event.kind == "tool"
                and event.call is not None
                and event.call.name == "run"
                and event.outcome is not None
                and event.outcome.ok
                and event.outcome.exit_code == 0
            ):
                command = str(event.call.args.get("command") or "")
                try:
                    self.project_facts.record_success(
                        project,
                        str(event.call.args.get("path") or "."),
                        command,
                    )
                except (OSError, ValueError):
                    pass
            if (
                project
                and event.kind == "tool"
                and event.call is not None
                and event.outcome is not None
            ):
                if event.call.name == "edit" and event.outcome.ok and event.outcome.changed:
                    rel = str(event.call.args.get("path") or "")
                    update_checkpoint(lambda store, item: store.record_edit(item, rel))
                elif event.call.name == "run":
                    command = str(event.call.args.get("command") or "")
                    cwd = str(event.call.args.get("path") or ".")
                    ok = event.outcome.ok and event.outcome.exit_code == 0
                    update_checkpoint(
                        lambda store, item: store.record_run(
                            item,
                            command=command,
                            cwd=cwd,
                            ok=ok,
                        )
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
            with state.lock:
                state.pending_shell[approval_id] = pending
            state.emit(pending["ui_event"])

        try:
            supervisor = getattr(state, "provider_supervisor", None)
            self_repair = getattr(state, "self_repair", None)

            def record_provider_failure(pid: str, failure: ProviderFailure) -> None:
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

            preflight_tried: set[str] = set()
            preflight_switches = 0
            if supervisor is not None:
                supervisor.prepare_user_selected(provider_id)
            if supervisor is not None and not supervisor.is_available(provider_id):
                replacement_id = supervisor.select(
                    "",
                    provider_failover_order(),
                    excluded=(provider_id,),
                )
                if replacement_id is not None:
                    provider_id = replacement_id
                    state.switch_run_provider(run_id, provider_id)
                    preflight_switches = 1
                else:
                    raise RuntimeError("selected provider is unavailable")
            while True:
                preflight_tried.add(provider_id)
                try:
                    provider = state.get_provider(provider_id)
                except cancellation.TaskCancelled:
                    raise
                except Exception as connect_error:
                    failure = self.capture_provider_failure(
                        model=PROVIDER_LABELS.get(provider_id, provider_id),
                        action="connect",
                        page=None,
                        error=connect_error,
                    )
                    record_provider_failure(provider_id, failure)
                    if preflight_switches >= 2:
                        raise ProviderActionError(failure) from connect_error
                    replacement_id = (
                        supervisor.select(
                            "",
                            provider_failover_order(),
                            excluded=preflight_tried,
                        )
                        if supervisor is not None
                        else next(
                            (
                                item
                                for item in provider_failover_order()
                                if item not in preflight_tried
                            ),
                            None,
                        )
                    )
                    if replacement_id is None:
                        raise ProviderActionError(failure) from connect_error
                    provider_id = replacement_id
                    preflight_switches += 1
                    state.switch_run_provider(run_id, provider_id)
                    continue
                if (
                    supervisor is None
                    or not supervisor.needs_canary(provider_id)
                    or run_half_open_canary(provider_id, provider, supervisor)
                ):
                    break
                try:
                    provider.close()
                except Exception:
                    pass
                if preflight_switches >= 2:
                    raise RuntimeError("no healthy provider available after canary failure")
                replacement_id = supervisor.select(
                    "",
                    provider_failover_order(),
                    excluded=preflight_tried,
                )
                if replacement_id is None:
                    raise RuntimeError("no healthy provider available after canary failure")
                provider_id = replacement_id
                preflight_switches += 1
                state.switch_run_provider(run_id, provider_id)
            mode = "research" if task_kind == "research" else ("project" if project else "chat")
            project_text = str(Path(project).expanduser().resolve()) if project else ""
            conversation = state.conversation_for(session_id)
            provider_session_changed = state.provider_session_changed(
                provider_id,
                session_id,
            )
            can_summarize_current_chat = (
                conversation.initialized
                and provider_id == conversation.provider_id
                and not provider_session_changed
            )
            fresh_chat, handoff = conversation.plan_request(
                provider_id=provider_id,
                mode=mode,
                project=project_text,
                force_rollover=continue_task or provider_session_changed,
                next_prompt=task,
            )
            if fresh_chat and can_summarize_current_chat:
                handoff = conversation.prepare_model_handoff(provider.send)
            prior_snapshot = conversation.snapshot
            recovered_owner_prompt = ""
            visible_excerpt = ""
            if fresh_chat or task_kind in {"research", "hybrid"}:
                try:
                    visible_excerpt = state.visible_session_excerpt(
                        session_id,
                        current_request=task,
                    )
                except Exception:
                    visible_excerpt = ""
            research_handoff = ""
            if task_kind in {"research", "hybrid"}:
                if handoff or visible_excerpt:
                    research_handoff = render_recovered_handoff(
                        prior_snapshot,
                        visible_excerpt,
                    )
                elif prior_snapshot.to_payload():
                    research_handoff = render_handoff(prior_snapshot)
            conversation.update_snapshot(ConversationSnapshot(
                mode=mode,
                goal=prior_snapshot.goal or task,
                project=project_text,
                provider_id=prior_snapshot.provider_id,
                changed_files=prior_snapshot.changed_files,
                checks_passed=prior_snapshot.checks_passed,
                summary=prior_snapshot.summary,
                blocker=prior_snapshot.blocker,
                latest_user=task if mode == "chat" else "",
                latest_reply=prior_snapshot.latest_reply if mode == "chat" else "",
                conversation_summary=prior_snapshot.conversation_summary,
            ))
            if fresh_chat:
                if handoff or visible_excerpt:
                    handoff = render_recovered_handoff(
                        prior_snapshot,
                        visible_excerpt,
                    )
                if visible_excerpt:
                    recovered_owner_prompt = handoff
            frame = _RunFrame(
                request=request,
                run_id=run_id,
                task_kind=task_kind,
                provider=provider,
                provider_id=provider_id,
                project_text=project_text,
                conversation=conversation,
                fresh_chat=fresh_chat,
                handoff=handoff,
                research_handoff=research_handoff,
                prior_snapshot=prior_snapshot,
                recovered_owner_prompt=recovered_owner_prompt,
                provider_session_changed=provider_session_changed,
                preflight_tried=preflight_tried,
                preflight_switches=preflight_switches,
            )
            hooks = _RunHooks(
                on_event=on_event,
                on_shell_request=on_shell_request,
                update_checkpoint=update_checkpoint,
                record_provider_failure=record_provider_failure,
                provider_failover_order=provider_failover_order,
                supervisor=supervisor,
            )
            if task_kind == "research":
                outcome = self._run_research_mode(frame, hooks)
            elif task_kind == "hybrid":
                outcome = self._run_hybrid_mode(frame, work, hooks)
            elif project:
                outcome = self._run_project_mode(frame, work, hooks)
            else:
                outcome = self._run_chat_mode(frame)
            state.finish_run(run_id, outcome.event)
        except (provider_controls.ControlTeachCancelled, cancellation.TaskCancelled):
            current_id = current_provider_id()
            state.set_provider_session(current_id, None)
            update_checkpoint(
                lambda store, item: store.set_status(item, "interrupted", "stopped")
            )
            current_conversation = (
                frame.conversation
                if frame is not None
                else (conversation if "conversation" in locals() else None)
            )
            if current_conversation is not None:
                current_conversation.update_snapshot(replace(
                    current_conversation.snapshot,
                    provider_id=current_id,
                    blocker="stopped",
                ))
            state.finish_run(run_id, {
                "type": "task_done",
                "run_id": run_id,
                "session_id": session_id,
                "summary": "",
                "stop_reason": "stopped",
                "turns": 0,
                "max_turns": max_turns,
                "provider": current_id,
                "provider_failure": None,
            })
        except Exception as exc:
            current_id = current_provider_id()
            current_item = current_provider()
            update_checkpoint(
                lambda store, item: store.set_status(item, "interrupted", "error")
            )
            current_conversation = (
                frame.conversation
                if frame is not None
                else (conversation if "conversation" in locals() else None)
            )
            if current_conversation is not None:
                current_conversation.update_snapshot(replace(
                    current_conversation.snapshot,
                    provider_id=current_id,
                    blocker=str(exc),
                ))
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
            state.finish_run(run_id, {
                "type": "task_done",
                "run_id": run_id,
                "session_id": session_id,
                "summary": f"ERROR: {exc}",
                "stop_reason": "error",
                "turns": 0,
                "max_turns": max_turns,
                "provider": current_id,
                "provider_failure": failure.to_dict() if failure else None,
            })
        finally:
            cancellation.set_event(previous_cancel_event)
            provider_controls.end_task_context()
            try:
                current_item = current_provider()
                if current_item is not None:
                    current_item.close()
            except Exception:
                pass

    def _run_research_mode(self, frame: _RunFrame, hooks: _RunHooks) -> _ModeOutcome:
        state = self.state
        request = frame.request
        if frame.provider is None:
            raise RuntimeError("provider is not connected")
        result = self._run_research_task(
            provider=frame.provider,
            session_id=request.session_id,
            project=frame.project_text,
            task=request.task,
            max_turns=request.max_turns,
            on_event=hooks.on_event,
            stop_flag=state.stop_flag,
            provider_id=frame.provider_id,
            run_id=frame.run_id,
            chat_handoff=frame.research_handoff,
        )
        state.set_provider_session(
            frame.provider_id,
            None if result.stop_reason == "stopped" else request.session_id,
        )
        frame.conversation.begin_window(
            frame.provider_id,
            "research",
            frame.project_text,
        )
        frame.conversation.record_exchange(
            request.task,
            result.summary,
            replace(
                frame.conversation.snapshot,
                mode="research",
                goal=request.task,
                project=frame.project_text,
                provider_id=frame.provider_id,
                blocker="" if result.stop_reason == "done" else result.summary,
                latest_user=request.task,
                latest_reply=result.summary,
                summary=result.summary,
            ),
        )
        receipt = {
            "text": result.receipt,
            "created": result.notes_created,
            "updated": result.notes_updated,
            "synthesis_id": result.synthesis_id,
        }
        return _ModeOutcome({
            "type": "task_done",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "summary": result.summary,
            "stop_reason": result.stop_reason,
            "turns": result.turns,
            "max_turns": request.max_turns,
            "provider": frame.provider_id,
            "mode": "research",
            "receipt": receipt,
            "research": _research_payload(result),
        })

    def _run_hybrid_mode(
        self,
        frame: _RunFrame,
        work: _RunWork,
        hooks: _RunHooks,
    ) -> _ModeOutcome:
        request = frame.request
        if frame.provider is None:
            raise RuntimeError("provider is not connected")
        research_result = self._run_research_task(
            provider=frame.provider,
            session_id=request.session_id,
            project=frame.project_text,
            task=request.task,
            max_turns=max(1, min(request.max_turns, 18)),
            on_event=hooks.on_event,
            stop_flag=self.state.stop_flag,
            provider_id=frame.provider_id,
            run_id=frame.run_id,
            chat_handoff=frame.research_handoff,
        )
        if research_result.stop_reason != "done":
            return _ModeOutcome({
                "type": "task_done",
                "run_id": frame.run_id,
                "session_id": request.session_id,
                "summary": research_result.summary,
                "stop_reason": research_result.stop_reason,
                "turns": research_result.turns,
                "max_turns": request.max_turns,
                "provider": frame.provider_id,
                "mode": "research",
                "receipt": {"text": research_result.receipt},
                "research": _research_payload(research_result),
            })
        frame.fresh_chat = True
        frame.handoff = ""
        frame.conversation.update_snapshot(replace(
            frame.conversation.snapshot,
            mode="research",
            goal=request.task,
            project=frame.project_text,
            provider_id=frame.provider_id,
            summary=research_result.summary,
            blocker="",
            latest_user=request.task,
            latest_reply=research_result.summary,
        ))
        return self._run_project_mode(
            frame,
            work,
            hooks,
            research_result=research_result,
        )

    def _run_chat_mode(self, frame: _RunFrame) -> _ModeOutcome:
        state = self.state
        request = frame.request
        if frame.provider is None:
            raise RuntimeError("provider is not connected")
        if frame.fresh_chat:
            frame.provider.new_chat()
        prompt = (
            render_continuation_prompt(frame.handoff, request.task)
            if frame.handoff
            else request.task
        )
        consulted = None
        if self.run_consensus is not None:
            compact_context = (
                render_handoff(frame.prior_snapshot)
                if frame.fresh_chat and frame.handoff
                else (
                    render_handoff(frame.conversation.snapshot)
                    if frame.conversation.initialized
                    else ""
                )
            )
            try:
                consulted = self.run_consensus(
                    selected_provider=frame.provider,
                    selected_provider_id=frame.provider_id,
                    task=request.task,
                    context=compact_context,
                    draft_first=True,
                    owner_prompt=frame.recovered_owner_prompt,
                )
            except cancellation.TaskCancelled:
                raise
            except Exception:
                state.set_provider_session(frame.provider_id, None)
                raise
        reply = consulted.answer if consulted is not None else frame.provider.send(prompt)
        if frame.fresh_chat:
            frame.conversation.begin_window(frame.provider_id, "chat")
        state.set_provider_session(
            frame.provider_id,
            None if consulted is not None and consulted.degraded else request.session_id,
        )
        frame.conversation.record_exchange(
            prompt,
            reply,
            replace(
                frame.conversation.snapshot,
                provider_id=frame.provider_id,
                blocker="",
                latest_user=request.task,
                latest_reply=reply,
            ),
        )
        state.emit({
            "type": "reply",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "text": reply,
        })
        result = RunResult(reply, "done", 1)
        return _ModeOutcome({
            "type": "task_done",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "summary": result.summary,
            "stop_reason": result.stop_reason,
            "turns": result.turns,
            "max_turns": request.max_turns,
            "provider": frame.provider_id,
            "mode": "chat",
        })

    def _run_project_mode(
        self,
        frame: _RunFrame,
        work: _RunWork,
        hooks: _RunHooks,
        *,
        research_result=None,
    ) -> _ModeOutcome:
        state = self.state
        request = frame.request
        project = request.project
        if project is None:
            raise RuntimeError("project mode requires a project")
        context_builder = ProjectTaskContextBuilder(
            project_facts=self.project_facts,
            work_checkpoints=self.work_checkpoints,
            knowledge_store=self.knowledge_store,
        )
        project_context = context_builder.build(
            project=project,
            task=request.task,
            session_id=request.session_id,
            run_id=frame.run_id,
            continue_task=request.continue_task,
            provider_session_changed=frame.provider_session_changed,
        )
        verified_facts = project_context.verified_facts
        verification_verified_commands = (
            project_context.verification_verified_commands
        )
        verification_candidates = project_context.verification_candidates
        resumed_verification_commands = (
            project_context.resumed_verification_commands
        )
        project_map = project_context.project_map
        work.work_checkpoint = project_context.checkpoint.item
        checkpoint_prompt = project_context.checkpoint.prompt
        resumed_changed_files = project_context.checkpoint.changed_files
        resumed_successful_checks = project_context.checkpoint.successful_checks
        work.evidence.seed_checks(project_context.checkpoint.seed_checks)
        agent_task = request.task
        change_brief: ChangeBrief | None = None
        agent_fresh_chat = frame.fresh_chat
        has_user_files = _project_has_user_files(project)
        used_project_audit = False
        if self.run_consensus is not None and not has_user_files:
            context = render_project_context(
                frame.conversation.snapshot,
                verified_facts,
                project_map=project_map,
            )
            try:
                planned = self.run_consensus(
                    selected_provider=frame.provider,
                    selected_provider_id=frame.provider_id,
                    task=request.task,
                    context=context,
                    plan=True,
                    draft_first=True,
                )
            except cancellation.TaskCancelled:
                raise
            except Exception:
                state.set_provider_session(frame.provider_id, None)
                agent_fresh_chat = True
                planned = None
            if planned is not None:
                change_brief = new_project_change_brief(request.task, planned.answer)
                agent_task = change_brief.apply_to_task(request.task)
                agent_fresh_chat = True
        elif self.run_project_audit is not None and has_user_files:
            context = render_project_context(
                frame.conversation.snapshot,
                verified_facts,
                project_map=project_map,
            )
            try:
                reports = self.run_project_audit(
                    project=project,
                    selected_provider=frame.provider,
                    selected_provider_id=frame.provider_id,
                    task=request.task,
                    context=context,
                )
            except cancellation.TaskCancelled:
                raise
            except Exception:
                reports = ()
            if reports:
                change_brief = project_audit_change_brief(request.task, reports)
                agent_task = change_brief.apply_to_task(request.task)
                used_project_audit = True
        key = str(Path(project).expanduser().resolve())
        tracker = state.change_tracker_for(
            key,
            persistent=not self.is_git_repository(key),
        )
        tried_writers = set(frame.preflight_tried)

        def refresh_checkpoint_view() -> CheckpointView:
            nonlocal checkpoint_prompt
            nonlocal resumed_changed_files
            nonlocal resumed_successful_checks
            refreshed = context_builder.refresh_checkpoint(work.work_checkpoint)
            if refreshed.item is None:
                checkpoint_prompt = ""
                resumed_changed_files = ()
                resumed_successful_checks = ()
            else:
                work.work_checkpoint = refreshed.item
                checkpoint_prompt = refreshed.prompt
                resumed_changed_files = refreshed.changed_files
                resumed_successful_checks = refreshed.successful_checks
            if refreshed.workspace_changed:
                work.evidence.invalidate_checks()
            return CheckpointView(
                prompt=checkpoint_prompt,
                changed_files=resumed_changed_files,
                successful_checks=resumed_successful_checks,
            )

        def run_one_writer_attempt(
            spec: WriterAttempt,
            note_turn: Callable[[int], None],
        ) -> RunResult:
            def on_writer_event(event: RunEvent) -> None:
                note_turn(event.turn)
                hooks.on_event(event)

            return self.agent_run(
                spec.provider,
                Path(project),
                spec.task,
                max_turns=spec.remaining_turns,
                on_event=on_writer_event,
                on_shell_request=hooks.on_shell_request,
                stop_flag=state.stop_flag,
                fresh_chat=spec.fresh_chat,
                strict_fresh_chat=spec.strict_fresh_chat,
                change_tracker=tracker,
                conversation=frame.conversation,
                provider_id=spec.provider_id,
                handoff=spec.handoff,
                project_facts=verified_facts,
                research_context=project_context.research_context,
                project_map=project_map,
                work_checkpoint=spec.checkpoint.prompt,
                verification_candidates=verification_candidates,
                verification_candidate_loader=lambda: (
                    safe_verification_candidates(
                        project,
                        verification_verified_commands,
                        resumed_verification_commands,
                    )
                ),
                verification_changed_files=spec.checkpoint.changed_files,
                verification_successful_checks=(
                    spec.checkpoint.successful_checks
                ),
            )

        def select_next_writer(excluded: set[str]) -> str | None:
            if hooks.supervisor is not None:
                return hooks.supervisor.select(
                    "",
                    hooks.provider_failover_order(),
                    excluded=excluded,
                )
            return next(
                (
                    item
                    for item in hooks.provider_failover_order()
                    if item not in excluded
                ),
                None,
            )

        def capture_writer_failure(
            pid: str,
            action: str,
            error: BaseException,
        ) -> ProviderFailure:
            return self.capture_provider_failure(
                model=PROVIDER_LABELS.get(pid, pid),
                action=action,
                page=None,
                error=error,
            )

        def on_writer_switch(next_provider_id: str) -> None:
            state.switch_run_provider(frame.run_id, next_provider_id)
            frame.conversation.update_snapshot(replace(
                frame.conversation.snapshot,
                provider_id=next_provider_id,
                blocker="",
            ))

        failover = WriterFailoverRunner(
            provider=frame.provider,
            provider_id=frame.provider_id,
            switches=frame.preflight_switches,
            tried=tried_writers,
            attempt=run_one_writer_attempt,
            select_next=select_next_writer,
            connect=state.get_provider,
            close=lambda item: item.close(),
            needs_canary=(
                hooks.supervisor.needs_canary
                if hooks.supervisor is not None
                else (lambda _pid: False)
            ),
            run_canary=(
                lambda pid, item: run_half_open_canary(pid, item, hooks.supervisor)
            ),
            capture_failure=capture_writer_failure,
            record_failure=hooks.record_provider_failure,
            record_success=(
                hooks.supervisor.record_success
                if hooks.supervisor is not None
                else (lambda _pid: None)
            ),
            clear_session=lambda pid: state.set_provider_session(pid, None),
            on_switch=on_writer_switch,
            refresh_checkpoint=refresh_checkpoint_view,
            stopped=state.stop_flag.is_set,
        )

        try:
            result = failover.run(
                task=agent_task,
                turn_budget=request.max_turns,
                fresh=agent_fresh_chat,
                handoff=frame.handoff,
                checkpoint=CheckpointView(
                    prompt=checkpoint_prompt,
                    changed_files=resumed_changed_files,
                    successful_checks=resumed_successful_checks,
                ),
            )
        finally:
            frame.provider = failover.provider
            frame.provider_id = failover.provider_id
            frame.preflight_switches = failover.switches
        if (
            project_context.checkpoint.resumed
            and work.work_checkpoint is not None
            and not result.changed
            and not result.checks_ran
            and work.evidence.has_successful_checks
        ):
            result = replace(result, checks_passed=True)
        checkpoint_changed = bool(
            work.work_checkpoint is not None and work.work_checkpoint.changed_files
        )
        task_changed = result.changed or checkpoint_changed
        task_changes = self.collect_changes(project, tracker)
        collected_changed = change_state(task_changes)
        task_changes_dirty = collected_changed is None
        if collected_changed is not None:
            task_changed = collected_changed
        if result.stop_reason == "done":
            hooks.update_checkpoint(
                lambda store, item: store.set_status(item, "ready_for_review")
            )
        state.set_provider_session(
            frame.provider_id,
            None if result.stop_reason == "stopped" else request.session_id,
        )
        if (
            self.run_consensus is not None
            and not used_project_audit
            and result.stop_reason == "done"
            and not result.changed
            and not state.stop_flag.is_set()
        ):
            context = render_project_context(
                frame.conversation.snapshot,
                verified_facts,
                draft=result.summary,
                project_map=project_map,
            )
            try:
                consulted = self.run_consensus(
                    selected_provider=frame.provider,
                    selected_provider_id=frame.provider_id,
                    task=request.task,
                    context=context,
                    draft=result.summary,
                )
            except cancellation.TaskCancelled:
                raise
            except Exception:
                state.set_provider_session(frame.provider_id, None)
                consulted = None
            if consulted is not None:
                if consulted.degraded:
                    state.set_provider_session(frame.provider_id, None)
                result = replace(result, summary=consulted.answer)
        review_coordinator = ReviewCoordinator(self.collect_changes)

        def render_review_change_brief() -> str:
            return (
                change_brief.render(audience="reviewer")
                if change_brief is not None
                else ""
            )

        def refresh_review_project_map() -> str:
            nonlocal verified_facts
            nonlocal project_map
            nonlocal verification_candidates
            verified_facts = (
                self.project_facts.render(project)
                if self.project_facts is not None
                else ""
            )
            verification_candidates = safe_verification_candidates(
                project,
                verification_verified_commands,
                resumed_verification_commands,
            )
            project_map = safe_project_map(
                project,
                verified_facts,
                request.task,
                verification_candidate_lines(verification_candidates),
            )
            return project_map

        def close_writer_for_review() -> None:
            if frame.provider is not None:
                try:
                    frame.provider.close()
                except Exception:
                    pass
            frame.provider = None
            failover.provider = None

        def repair_writer(
            followup: str,
            checkpoint: CheckpointView,
        ) -> RunResult:
            try:
                return failover.run(
                    task=followup,
                    turn_budget=min(request.max_turns, self.review_fix_turns),
                    fresh=False,
                    handoff="",
                    checkpoint=checkpoint,
                )
            finally:
                frame.provider = failover.provider
                frame.provider_id = failover.provider_id
                frame.preflight_switches = failover.switches

        def set_checkpoint_status(status: str) -> None:
            hooks.update_checkpoint(
                lambda store, item: store.set_status(
                    item,
                    status,
                )
            )

        def emit_review_unavailable() -> None:
            state.emit({
                "type": "review",
                "session_id": request.session_id,
                "text": "Unavailable. Continued with one model.",
            })

        review_cycle = review_coordinator.run_cycle(
            project=project,
            tracker=tracker,
            session_id=request.session_id,
            task=request.task,
            result=result,
            task_changed=task_changed,
            changes=task_changes,
            changes_dirty=task_changes_dirty,
            writer_id=frame.provider_id,
            recent_log="\n".join(work.recent_events[-self.review_log_lines:]),
            render_change_brief=render_review_change_brief,
            execution_evidence=work.evidence.render_for_review(),
            successful_checks=work.evidence.successful_checks,
            checkpoint_prompt=checkpoint_prompt,
            checks_before_review_followup=(
                work.evidence.has_successful_checks
                or (
                    not work.evidence.observed_tool_events
                    and result.checks_passed
                )
            ),
            stop_requested=state.stop_flag.is_set,
            refresh_project_map=refresh_review_project_map,
            build_verification_map=lambda changes, current_project_map: (
                _safe_verification_map(
                    project,
                    changes,
                    work.evidence.successful_checks,
                    current_project_map,
                    selected_verification_candidate_lines(
                        verification_candidates,
                        _changed_file_paths(changes),
                    ),
                )
            ),
            run_review=self.run_review,
            close_writer_for_review=close_writer_for_review,
            repair_writer=repair_writer,
            set_checkpoint_status=set_checkpoint_status,
            emit_review_unavailable=emit_review_unavailable,
        )
        result = review_cycle.result
        task_changed = review_cycle.task_changed
        task_changes = review_cycle.changes
        task_changes_dirty = review_cycle.changes_dirty
        if task_changes is None or task_changes_dirty:
            task_changes = self.collect_changes(project, tracker)
        collected_changed = change_state(task_changes)
        if collected_changed is not None:
            task_changed = collected_changed
        files = tuple(
            str(item.get("path") or "")
            for item in (task_changes.get("files") or [])
            if item.get("path")
        )
        verification_candidates = safe_verification_candidates(
            project,
            verification_verified_commands,
            resumed_verification_commands,
        )
        if result.stop_reason == "done" and task_changed and files:
            selected_check = select_verification_candidate(
                verification_candidates,
                files,
            )
            if selected_check is not None and work.evidence.observed_tool_events:
                relevant_green = any(
                    check_covers_selected_candidate(
                        selected_check,
                        item.command,
                        item.cwd,
                        files,
                    )
                    for item in work.evidence.successful_checks
                )
                result = replace(result, checks_passed=relevant_green)
        receipt = build_task_receipt(
            task_changes,
            checks_passed=result.checks_passed,
        )
        facts_write_required = (
            self.project_facts is not None
            and result.stop_reason == "done"
            and task_changed
            and result.checks_passed
            and work.evidence.has_successful_checks
            and files
        )
        facts_write_succeeded = not facts_write_required
        if facts_write_required:
            try:
                fact_task = (
                    work.work_checkpoint.original_task
                    if project_context.checkpoint.resumed
                    and work.work_checkpoint is not None
                    else request.task
                )
                facts_write_succeeded = (
                    self.project_facts.record_successful_change(
                        project,
                        task=fact_task,
                        files=files,
                        checks=work.evidence.successful_checks,
                        receipt=receipt.text,
                    )
                )
            except (OSError, ValueError):
                facts_write_succeeded = False
        if facts_write_succeeded and facts_write_required:
            self._record_project_memory(
                project=project,
                session_id=request.session_id,
                task=request.task,
                files=files,
                receipt=receipt.text,
                checks=work.evidence.successful_checks,
            )
        if self.work_checkpoints is not None and work.work_checkpoint is not None:
            if result.stop_reason == "done" and facts_write_succeeded:
                try:
                    self.work_checkpoints.delete(request.session_id)
                    work.work_checkpoint = None
                except OSError:
                    pass
            elif result.stop_reason != "done":
                hooks.update_checkpoint(
                    lambda store, item: store.set_status(
                        item,
                        "interrupted",
                        result.stop_reason,
                    )
                )
        frame.conversation.update_snapshot(replace(
            frame.conversation.snapshot,
            provider_id=frame.provider_id,
            changed_files=files,
            checks_passed=result.checks_passed,
            summary=result.summary,
            blocker="" if result.stop_reason == "done" else result.summary,
        ))
        event = {
            "type": "task_done",
            "run_id": frame.run_id,
            "session_id": request.session_id,
            "summary": result.summary,
            "stop_reason": result.stop_reason,
            "turns": result.turns,
            "max_turns": request.max_turns,
            "provider": frame.provider_id,
            "changed": task_changed,
            "receipt": receipt.to_dict(),
        }
        if task_changed and task_changes and task_changes.get("ok"):
            event["changes"] = {
                "changed_count": task_changes.get("changed_count", 0),
                "files": task_changes.get("files", [])[:3],
                "mode": task_changes.get("mode"),
                "project": project,
            }
        if research_result is not None:
            event["research"] = _research_payload(research_result)
        return _ModeOutcome(event)

    def _run_research_task(
        self,
        *,
        provider,
        session_id: str,
        project: str,
        task: str,
        max_turns: int,
        on_event: Callable[[RunEvent], None],
        stop_flag,
        provider_id: str = "",
        run_id: str = "",
        chat_handoff: str = "",
    ):
        if self.knowledge_store is None:
            raise RuntimeError("Research is not configured")
        search = self.search_factory()
        try:
            runner = ResearchRunner(
                provider,
                search,
                self.knowledge_store,
                max_turns=max_turns,
                should_stop=stop_flag.is_set if stop_flag is not None else None,
                session_id=session_id,
                project=project,
                chat_handoff=chat_handoff,
                review_advisors=(
                    (lambda pack: self.run_research_advisors(
                        selected_provider=provider,
                        selected_provider_id=provider_id,
                        pack=pack,
                    ))
                    if self.run_research_advisors is not None
                    else None
                ),
            )
            for event in runner.run(task):
                on_event(event)
            if runner.result is None:
                raise RuntimeError("research finished without a result")
            recorder = getattr(self.state, "record_research_changes", None)
            if callable(recorder) and run_id:
                recorder(run_id, runner.changes)
            return runner.result
        finally:
            try:
                search.close()
            except Exception:
                pass

    def _record_project_memory(
        self,
        *,
        project: str,
        session_id: str,
        task: str,
        files: tuple[str, ...],
        receipt: str,
        checks: tuple[object, ...],
    ) -> None:
        if self.knowledge_store is None:
            return
        try:
            brief = KnowledgeBriefBuilder(self.knowledge_store).build_for_session(session_id)
            sources = [brief.synthesis_id] if brief.synthesis_id else []
            impl = KnowledgeNote.create(
                type="implementation",
                title=task[:120] or "Project implementation",
                body=(
                    "Implemented project task.\n\n"
                    f"Files changed:\n{_bullet_lines(files)}\n\n"
                    f"Receipt:\n{receipt}"
                ),
                tags=["project", "implementation", f"session:{session_id}"],
                sources=sources,
                session_id=session_id,
                project=str(Path(project).expanduser().resolve()),
            )
            self.knowledge_store.write_note(impl)
            if checks:
                verification = KnowledgeNote.create(
                    type="verification",
                    title=f"Verification for {task[:80] or 'project task'}",
                    body="Successful checks:\n" + _bullet_lines(
                        tuple(f"{item.command} (cwd {item.cwd})" for item in checks)
                    ),
                    tags=["project", "verification", f"session:{session_id}"],
                    sources=[impl.id],
                    session_id=session_id,
                    project=str(Path(project).expanduser().resolve()),
                )
                self.knowledge_store.write_note(verification)
                self.knowledge_store.link(impl.id, verification.id, "verifies")
            if brief.synthesis_id:
                self.knowledge_store.link(brief.synthesis_id, impl.id, "implements")
        except (OSError, ValueError):
            return

    @staticmethod
    def _ui_event(run_id: str, session_id: str, event: RunEvent) -> dict | None:
        if event.kind == "turn":
            payload = {"type": "turn", "run_id": run_id, "session_id": session_id, "turn": event.turn}
            if event.note:
                payload["note"] = event.note
            return payload
        if event.kind == "info":
            text = event.message
            names = str(event.metadata.get("names") or "")
            if names:
                text = f"{text}: {names}"
            return {"type": "info", "run_id": run_id, "session_id": session_id, "text": text}
        if event.kind == "tool_start" and event.call is not None:
            path = str(event.call.args.get("path") or "")
            display_kind, display_path = _display_tool(event.call.name, event.call.args, path)
            tool_index = int(event.metadata.get("tool_index") or 0)
            return {
                "type": "tool_started",
                "run_id": run_id,
                "session_id": session_id,
                "turn": event.turn,
                "tool_id": f"{event.turn}:{tool_index}",
                "kind": display_kind,
                "path": display_path,
                "activity": event.message,
            }
        if event.kind != "tool" or event.call is None or event.outcome is None:
            return None
        path = str(event.call.args.get("path") or "")
        display_kind, display_path = _display_tool(event.call.name, event.call.args, path)
        result = event.outcome.first_line(200)
        tool_index = int(event.metadata.get("tool_index") or 0)
        status = str(getattr(event.outcome, "status", "") or ("ok" if event.outcome.ok else "error"))
        return {
            "type": "tool",
            "run_id": run_id,
            "session_id": session_id,
            "turn": event.turn,
            "tool_id": f"{event.turn}:{tool_index}",
            "kind": display_kind,
            "path": display_path,
            "result": result,
            "status": status,
            "error": status == "error",
        }
