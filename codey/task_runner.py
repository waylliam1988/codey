"""Task orchestration independent of HTTP request handling."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

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
    ConversationSnapshot,
    render_continuation_prompt,
    render_handoff,
    render_recovered_handoff,
)
from codey.project_facts import ProjectFactsStore
from codey.project_map import render_project_map
from codey.providers import PROVIDER_LABELS
from codey.provider_diagnostics import ProviderActionError
from codey.provider_supervisor import run_half_open_canary
from codey.receipt import build_task_receipt
from codey.review import has_reviewable_changes, render_writer_followup
from codey.verification_map import render_verification_map
from codey.verification_policy import (
    VerificationCandidate,
    check_matches_candidate,
    discover_verification_candidates,
    select_verification_candidate,
)
from codey.work_checkpoint import (
    WorkCheckpoint,
    WorkCheckpointStore,
    render_work_checkpoint,
)


@dataclass(frozen=True)
class TaskRequest:
    session_id: str
    project: str | None
    task: str
    max_turns: int
    continue_task: bool
    provider_id: str
    run_id: str = ""


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


def _verification_candidates(
    project: str | Path,
    verified_commands: tuple[object, ...] = (),
    additional_commands: tuple[object, ...] = (),
) -> tuple[VerificationCandidate, ...]:
    try:
        return discover_verification_candidates(
            project,
            verified_commands + additional_commands,
        )
    except (OSError, TypeError, ValueError):
        return ()


def _verified_commands(
    project: str | Path,
    store: ProjectFactsStore | None,
) -> tuple[object, ...]:
    try:
        facts = store.load(project) if store is not None else None
        commands = getattr(facts, "commands", ())
        return tuple(commands) if isinstance(commands, (tuple, list)) else ()
    except (OSError, TypeError, ValueError):
        return ()


def _safe_project_map(project: str | Path, verified_facts: str, task: str = "") -> str:
    try:
        return render_project_map(project, verified_facts, task=task)
    except Exception:
        return ""


def _safe_verification_map(
    project: str | Path,
    changes: dict,
    checks: tuple[object, ...],
    project_map: str,
) -> str:
    try:
        return render_verification_map(
            project,
            changes,
            checks_after_last_change=checks,
            project_map=project_map,
        )
    except Exception:
        return ""


def _changed_state(changes: object) -> bool | None:
    """Return final diff state, or None when change collection was unavailable."""
    if not isinstance(changes, dict) or changes.get("ok") is not True:
        return None
    files = changes.get("files")
    changed_count = changes.get("changed_count")
    return bool(
        (isinstance(changed_count, int) and changed_count > 0)
        or (isinstance(files, list) and files)
    )


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
        project_facts: ProjectFactsStore | None = None,
        work_checkpoints: WorkCheckpointStore | None = None,
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
        self.capture_provider_failure = capture_provider_failure
        self.project_facts = project_facts
        self.work_checkpoints = work_checkpoints
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
            "mode": "agent" if project else "chat",
            "max_turns": max_turns,
            "continue_task": continue_task,
            "provider": provider_id,
        })

        recent_events: list[str] = []
        task_changes: dict | None = None
        task_changes_dirty = False
        evidence = ExecutionEvidence()
        work_checkpoint: WorkCheckpoint | None = None
        resumed_checkpoint = False

        def update_checkpoint(
            action: Callable[[WorkCheckpointStore, WorkCheckpoint], WorkCheckpoint],
        ) -> None:
            nonlocal work_checkpoint
            if self.work_checkpoints is None or work_checkpoint is None:
                return
            try:
                work_checkpoint = action(self.work_checkpoints, work_checkpoint)
            except (OSError, ValueError):
                pass

        def on_event(event: RunEvent) -> None:
            evidence.record(event)
            message = render_run_event(event)
            recent_events.append(message)
            if len(recent_events) > self.review_log_lines * 2:
                del recent_events[:self.review_log_lines]
            payload = self._ui_event(run_id, session_id, event)
            if payload is not None:
                state.emit(payload)
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
            approval_id = "shell_" + uuid.uuid4().hex[:12]
            pending = {
                "id": approval_id,
                "session_id": session_id,
                "project": project,
                "cwd": cwd_rel or ".",
                "command": command,
                "max_turns": max_turns,
                "provider": provider_id,
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
            }
            with state.lock:
                state.pending_shell[approval_id] = pending
            state.emit(pending["ui_event"])

        try:
            supervisor = getattr(state, "provider_supervisor", None)
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
                    if supervisor is not None:
                        supervisor.record_failure(provider_id, failure)
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
            mode = "project" if project else "chat"
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
            task_changed = False
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
                visible_excerpt = ""
                try:
                    visible_excerpt = state.visible_session_excerpt(
                        session_id,
                        current_request=task,
                    )
                except Exception:
                    visible_excerpt = ""
                if handoff or visible_excerpt:
                    handoff = render_recovered_handoff(
                        prior_snapshot,
                        visible_excerpt,
                    )
                if visible_excerpt:
                    recovered_owner_prompt = handoff
            if project:
                verified_facts = (
                    self.project_facts.render(project)
                    if self.project_facts is not None
                    else ""
                )
                verification_verified_commands = _verified_commands(
                    project,
                    self.project_facts,
                )
                verification_candidates = _verification_candidates(
                    project,
                    verification_verified_commands,
                )
                verification_additional_commands: tuple[object, ...] = ()
                resumed_changed_files: tuple[str, ...] = ()
                resumed_successful_checks: tuple[VerificationCandidate, ...] = ()
                project_map = _safe_project_map(project, verified_facts, task)
                checkpoint_prompt = ""
                if self.work_checkpoints is not None:
                    try:
                        previous = self.work_checkpoints.load(session_id)
                        same_project = (
                            previous is not None
                            and previous.project == str(Path(project).expanduser().resolve())
                        )
                        resume_requested = continue_task or provider_session_changed
                        same_task = (
                            previous is not None
                            and previous.original_task.strip() == task.strip()
                        )
                        if same_project and resume_requested and (continue_task or same_task):
                            work_checkpoint = self.work_checkpoints.reconcile(previous)
                            evidence.seed_checks(
                                work_checkpoint.successful_checks_after_last_change
                            )
                            resumed_changed_files = tuple(
                                item.path for item in work_checkpoint.changed_files
                            )
                            resumed_successful_checks = tuple(
                                VerificationCandidate(item.command, item.cwd, "checkpoint")
                                for item in work_checkpoint.successful_checks_after_last_change
                            )
                            verification_candidates = _verification_candidates(
                                project,
                                verification_verified_commands,
                                work_checkpoint.successful_checks_after_last_change,
                            )
                            verification_additional_commands = (
                                work_checkpoint.successful_checks_after_last_change
                            )
                            checkpoint_prompt = render_work_checkpoint(work_checkpoint)
                            resumed_checkpoint = True
                        else:
                            work_checkpoint = self.work_checkpoints.start(
                                run_id=run_id,
                                session_id=session_id,
                                project=project,
                                task=task,
                            )
                    except (OSError, ValueError):
                        work_checkpoint = None
                agent_task = task
                change_brief: ChangeBrief | None = None
                agent_fresh_chat = fresh_chat
                has_user_files = _project_has_user_files(project)
                used_project_audit = False
                if self.run_consensus is not None and not has_user_files:
                    context = render_project_context(
                        conversation.snapshot,
                        verified_facts,
                        project_map=project_map,
                    )
                    try:
                        planned = self.run_consensus(
                            selected_provider=provider,
                            selected_provider_id=provider_id,
                            task=task,
                            context=context,
                            plan=True,
                            draft_first=True,
                        )
                    except cancellation.TaskCancelled:
                        raise
                    except Exception:
                        state.set_provider_session(provider_id, None)
                        agent_fresh_chat = True
                        planned = None
                    if planned is not None:
                        change_brief = new_project_change_brief(task, planned.answer)
                        agent_task = change_brief.apply_to_task(task)
                        agent_fresh_chat = True
                elif self.run_project_audit is not None and has_user_files:
                    context = render_project_context(
                        conversation.snapshot,
                        verified_facts,
                        project_map=project_map,
                    )
                    try:
                        reports = self.run_project_audit(
                            project=project,
                            selected_provider=provider,
                            selected_provider_id=provider_id,
                            task=task,
                            context=context,
                        )
                    except cancellation.TaskCancelled:
                        raise
                    except Exception:
                        reports = ()
                    if reports:
                        change_brief = project_audit_change_brief(task, reports)
                        agent_task = change_brief.apply_to_task(task)
                        used_project_audit = True
                key = str(Path(project).expanduser().resolve())
                tracker = state.change_tracker_for(
                    key,
                    persistent=not self.is_git_repository(key),
                )
                tried_writers = set(preflight_tried)
                writer_switches = preflight_switches

                def refresh_takeover_checkpoint() -> None:
                    nonlocal work_checkpoint
                    nonlocal checkpoint_prompt
                    nonlocal resumed_changed_files
                    nonlocal resumed_successful_checks
                    if self.work_checkpoints is None or work_checkpoint is None:
                        return
                    try:
                        work_checkpoint = self.work_checkpoints.reconcile(work_checkpoint)
                        if work_checkpoint.workspace_changed:
                            evidence.invalidate_checks()
                        checkpoint_prompt = render_work_checkpoint(work_checkpoint)
                        resumed_changed_files = tuple(
                            item.path for item in work_checkpoint.changed_files
                        )
                        resumed_successful_checks = tuple(
                            VerificationCandidate(item.command, item.cwd, "checkpoint")
                            for item in work_checkpoint.successful_checks_after_last_change
                        )
                    except (OSError, ValueError):
                        checkpoint_prompt = ""
                        resumed_changed_files = ()
                        resumed_successful_checks = ()

                def activate_next_writer(origin: ProviderActionError) -> None:
                    nonlocal provider
                    nonlocal provider_id
                    nonlocal writer_switches
                    while True:
                        next_provider_id = (
                            supervisor.select(
                                "",
                                provider_failover_order(),
                                excluded=tried_writers,
                            )
                            if supervisor is not None
                            else next(
                                (
                                    item
                                    for item in provider_failover_order()
                                    if item not in tried_writers
                                ),
                                None,
                            )
                        )
                        if next_provider_id is None:
                            raise origin
                        provider_id = next_provider_id
                        tried_writers.add(provider_id)
                        writer_switches += 1
                        state.switch_run_provider(run_id, provider_id)
                        conversation.update_snapshot(replace(
                            conversation.snapshot,
                            provider_id=provider_id,
                            blocker="",
                        ))
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
                            if supervisor is not None:
                                supervisor.record_failure(provider_id, failure)
                            if writer_switches >= 2:
                                raise ProviderActionError(failure) from connect_error
                            continue
                        if (
                            supervisor is not None
                            and supervisor.needs_canary(provider_id)
                            and not run_half_open_canary(
                                provider_id,
                                provider,
                                supervisor,
                            )
                        ):
                            try:
                                provider.close()
                            except Exception:
                                pass
                            if writer_switches >= 2:
                                raise origin
                            continue
                        refresh_takeover_checkpoint()
                        return

                def run_writer_with_failover(
                    writer_task: str,
                    turn_budget: int,
                    *,
                    fresh: bool,
                    factual_handoff: str = "",
                    changed_files: tuple[str, ...] = (),
                    successful_checks: tuple[VerificationCandidate, ...] = (),
                ) -> RunResult:
                    nonlocal provider
                    nonlocal writer_switches
                    turns_used = 0
                    current_fresh = fresh
                    current_handoff = factual_handoff
                    current_changed_files = changed_files
                    current_successful_checks = successful_checks
                    if provider is None:
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
                            if supervisor is not None:
                                supervisor.record_failure(provider_id, failure)
                            state.set_provider_session(provider_id, None)
                            error = ProviderActionError(failure)
                            if writer_switches >= 2:
                                raise error from connect_error
                            activate_next_writer(error)
                            current_fresh = True
                            current_changed_files = resumed_changed_files
                            current_successful_checks = resumed_successful_checks
                    while True:
                        attempt_turn = 0

                        def on_writer_event(event: RunEvent) -> None:
                            nonlocal attempt_turn
                            attempt_turn = max(attempt_turn, event.turn)
                            on_event(event)

                        try:
                            writer_result = self.agent_run(
                                provider,
                                Path(project),
                                writer_task,
                                max_turns=max(1, turn_budget - turns_used),
                                on_event=on_writer_event,
                                on_shell_request=on_shell_request,
                                stop_flag=state.stop_flag,
                                fresh_chat=current_fresh,
                                strict_fresh_chat=writer_switches > 0,
                                change_tracker=tracker,
                                conversation=conversation,
                                provider_id=provider_id,
                                handoff=current_handoff,
                                project_facts=verified_facts,
                                project_map=project_map,
                                work_checkpoint=checkpoint_prompt,
                                verification_candidates=verification_candidates,
                                verification_candidate_loader=lambda: (
                                    _verification_candidates(
                                        project,
                                        verification_verified_commands,
                                        verification_additional_commands,
                                    )
                                ),
                                verification_changed_files=current_changed_files,
                                verification_successful_checks=(
                                    current_successful_checks
                                ),
                            )
                            if supervisor is not None:
                                supervisor.record_success(provider_id)
                            return replace(
                                writer_result,
                                turns=min(turn_budget, turns_used + writer_result.turns),
                            )
                        except ProviderActionError as exc:
                            if state.stop_flag.is_set():
                                raise cancellation.TaskCancelled("task stopped") from exc
                            turns_used += max(1, attempt_turn)
                            if supervisor is not None:
                                supervisor.record_failure(provider_id, exc.failure)
                            state.set_provider_session(provider_id, None)
                            try:
                                provider.close()
                            except Exception:
                                pass
                            provider = None
                            if writer_switches >= 2 or turns_used >= turn_budget:
                                raise
                            activate_next_writer(exc)
                            current_fresh = True
                            current_handoff = ""
                            current_changed_files = resumed_changed_files
                            current_successful_checks = resumed_successful_checks

                result = run_writer_with_failover(
                    agent_task,
                    max_turns,
                    fresh=agent_fresh_chat,
                    factual_handoff=handoff,
                    changed_files=resumed_changed_files,
                    successful_checks=resumed_successful_checks,
                )
                if (
                    resumed_checkpoint
                    and work_checkpoint is not None
                    and not result.changed
                    and not result.checks_ran
                    and evidence.has_successful_checks
                ):
                    result = replace(result, checks_passed=True)
                checkpoint_changed = bool(
                    work_checkpoint is not None and work_checkpoint.changed_files
                )
                task_changed = result.changed or checkpoint_changed
                task_changes = self.collect_changes(project, tracker)
                collected_changed = _changed_state(task_changes)
                task_changes_dirty = collected_changed is None
                if collected_changed is not None:
                    task_changed = collected_changed
                if result.stop_reason == "done":
                    update_checkpoint(
                        lambda store, item: store.set_status(item, "ready_for_review")
                    )
                state.set_provider_session(
                    provider_id,
                    None if result.stop_reason == "stopped" else session_id,
                )
                if (
                    self.run_consensus is not None
                    and not used_project_audit
                    and result.stop_reason == "done"
                    and not result.changed
                    and not state.stop_flag.is_set()
                ):
                    context = render_project_context(
                        conversation.snapshot,
                        verified_facts,
                        draft=result.summary,
                        project_map=project_map,
                    )
                    try:
                        consulted = self.run_consensus(
                            selected_provider=provider,
                            selected_provider_id=provider_id,
                            task=task,
                            context=context,
                            draft=result.summary,
                        )
                    except cancellation.TaskCancelled:
                        raise
                    except Exception:
                        state.set_provider_session(provider_id, None)
                        consulted = None
                    if consulted is not None:
                        if consulted.degraded:
                            state.set_provider_session(provider_id, None)
                        result = replace(result, summary=consulted.answer)
                if (
                    result.stop_reason == "done"
                    and task_changed
                    and not state.stop_flag.is_set()
                    and task_changes_dirty
                ):
                    task_changes = self.collect_changes(project, tracker)
                    collected_changed = _changed_state(task_changes)
                    task_changes_dirty = collected_changed is None
                    if collected_changed is not None:
                        task_changed = collected_changed
                if (
                    result.stop_reason == "done"
                    and task_changed
                    and not state.stop_flag.is_set()
                ):
                    verified_facts = (
                        self.project_facts.render(project)
                        if self.project_facts is not None
                        else ""
                    )
                    project_map = _safe_project_map(project, verified_facts, task)
                    if has_reviewable_changes(task_changes):
                        verification_map = _safe_verification_map(
                            project,
                            task_changes,
                            evidence.successful_checks,
                            project_map,
                        )
                        rendered_change_brief = (
                            change_brief.render(audience="reviewer")
                            if change_brief is not None
                            else ""
                        )
                        try:
                            provider.close()
                        except Exception:
                            pass
                        provider = None
                        try:
                            reviewed = self.run_review(
                                session_id=session_id,
                                project=project,
                                task=task,
                                writer_summary=result.summary,
                                changes=task_changes,
                                recent_log="\n".join(recent_events[-self.review_log_lines:]),
                                execution_evidence=evidence.render_for_review(),
                                writer_id=provider_id,
                                change_brief=rendered_change_brief,
                                project_map=project_map,
                                verification_map=verification_map,
                            )
                        except cancellation.TaskCancelled:
                            raise
                        except Exception:
                            state.emit({
                                "type": "review",
                                "session_id": session_id,
                                "text": "Unavailable. Continued with one model.",
                            })
                            reviewed = None
                        if reviewed is not None:
                            _reviewer_id, review = reviewed
                            if not review.approved:
                                update_checkpoint(
                                    lambda store, item: store.set_status(
                                        item,
                                        "fixing_review",
                                    )
                                )
                                checks_before_review_followup = (
                                    evidence.has_successful_checks
                                    or (
                                        not evidence.observed_tool_events
                                        and result.checks_passed
                                    )
                                )
                                followup = render_writer_followup(
                                    task,
                                    review,
                                    change_brief=rendered_change_brief,
                                )
                                verified_facts = (
                                    self.project_facts.render(project)
                                    if self.project_facts is not None
                                    else ""
                                )
                                project_map = _safe_project_map(project, verified_facts, task)
                                result = run_writer_with_failover(
                                    followup,
                                    min(max_turns, self.review_fix_turns),
                                    fresh=False,
                                    changed_files=tuple(
                                        str(item.get("path") or "")
                                        for item in (task_changes.get("files") or [])
                                        if item.get("path")
                                    ),
                                    successful_checks=tuple(
                                        VerificationCandidate(
                                            item.command,
                                            item.cwd,
                                            "execution evidence",
                                        )
                                        for item in evidence.successful_checks
                                    ),
                                )
                                task_changes_dirty = True
                                if result.stop_reason == "done":
                                    update_checkpoint(
                                        lambda store, item: store.set_status(
                                            item,
                                            "ready_for_review",
                                        )
                                    )
                                if (
                                    result.stop_reason == "done"
                                    and not result.changed
                                    and checks_before_review_followup
                                    and not result.checks_ran
                                ):
                                    result = replace(result, checks_passed=True)
                                task_changed = task_changed or result.changed
            else:
                if fresh_chat:
                    provider.new_chat()
                prompt = render_continuation_prompt(handoff, task) if handoff else task
                consulted = None
                if self.run_consensus is not None:
                    compact_context = (
                        render_handoff(prior_snapshot)
                        if fresh_chat and handoff
                        else (
                            render_handoff(conversation.snapshot)
                            if conversation.initialized
                            else ""
                        )
                    )
                    try:
                        consulted = self.run_consensus(
                            selected_provider=provider,
                            selected_provider_id=provider_id,
                            task=task,
                            context=compact_context,
                            draft_first=True,
                            owner_prompt=recovered_owner_prompt,
                        )
                    except cancellation.TaskCancelled:
                        raise
                    except Exception:
                        state.set_provider_session(provider_id, None)
                        raise
                reply = consulted.answer if consulted is not None else provider.send(prompt)
                if fresh_chat:
                    conversation.begin_window(provider_id, "chat")
                state.set_provider_session(
                    provider_id,
                    None
                    if consulted is not None and consulted.degraded
                    else session_id,
                )
                conversation.record_exchange(
                    prompt,
                    reply,
                    replace(
                        conversation.snapshot,
                        provider_id=provider_id,
                        blocker="",
                        latest_user=task,
                        latest_reply=reply,
                    ),
                )
                state.emit({"type": "reply", "session_id": session_id, "text": reply})
                result = RunResult("", "done", 1)
            if project:
                if task_changes is None or task_changes_dirty:
                    task_changes = self.collect_changes(project, tracker)
                collected_changed = _changed_state(task_changes)
                if collected_changed is not None:
                    task_changed = collected_changed
                files = tuple(
                    str(item.get("path") or "")
                    for item in (task_changes.get("files") or [])
                    if item.get("path")
                )
                verification_candidates = _verification_candidates(
                    project,
                    verification_verified_commands,
                    verification_additional_commands,
                )
                if result.stop_reason == "done" and task_changed and files:
                    selected_check = select_verification_candidate(
                        verification_candidates,
                        files,
                    )
                    if selected_check is not None and evidence.observed_tool_events:
                        relevant_green = any(
                            check_matches_candidate(
                                selected_check,
                                item.command,
                                item.cwd,
                            )
                            for item in evidence.successful_checks
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
                    and evidence.has_successful_checks
                    and files
                )
                facts_write_succeeded = not facts_write_required
                if facts_write_required:
                    try:
                        fact_task = (
                            work_checkpoint.original_task
                            if resumed_checkpoint and work_checkpoint is not None
                            else task
                        )
                        facts_write_succeeded = (
                            self.project_facts.record_successful_change(
                                project,
                                task=fact_task,
                                files=files,
                                check_commands=[
                                    item.command for item in evidence.successful_checks
                                ],
                                receipt=receipt.text,
                            )
                        )
                    except (OSError, ValueError):
                        facts_write_succeeded = False
                if self.work_checkpoints is not None and work_checkpoint is not None:
                    if result.stop_reason == "done" and facts_write_succeeded:
                        try:
                            self.work_checkpoints.delete(session_id)
                            work_checkpoint = None
                        except OSError:
                            pass
                    elif result.stop_reason != "done":
                        update_checkpoint(
                            lambda store, item: store.set_status(
                                item,
                                "interrupted",
                                result.stop_reason,
                            )
                        )
                conversation.update_snapshot(replace(
                    conversation.snapshot,
                    provider_id=provider_id,
                    changed_files=files,
                    checks_passed=result.checks_passed,
                    summary=result.summary,
                    blocker="" if result.stop_reason == "done" else result.summary,
                ))
            else:
                receipt = None
            event = {
                "type": "task_done",
                "run_id": run_id,
                "session_id": session_id,
                "summary": result.summary,
                "stop_reason": result.stop_reason,
                "turns": result.turns,
                "max_turns": max_turns,
                "provider": provider_id,
            }
            if receipt is not None:
                event["changed"] = task_changed
                event["receipt"] = receipt.to_dict()
                if task_changed and task_changes and task_changes.get("ok"):
                    event["changes"] = {
                        "changed_count": task_changes.get("changed_count", 0),
                        "files": task_changes.get("files", [])[:3],
                        "mode": task_changes.get("mode"),
                        "project": project,
                    }
            state.finish_run(run_id, event)
        except (provider_controls.ControlTeachCancelled, cancellation.TaskCancelled):
            state.set_provider_session(provider_id, None)
            update_checkpoint(
                lambda store, item: store.set_status(item, "interrupted", "stopped")
            )
            if "conversation" in locals():
                conversation.update_snapshot(replace(
                    conversation.snapshot,
                    provider_id=provider_id,
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
                "provider": provider_id,
                "provider_failure": None,
            })
        except Exception as exc:
            update_checkpoint(
                lambda store, item: store.set_status(item, "interrupted", "error")
            )
            if "conversation" in locals():
                conversation.update_snapshot(replace(
                    conversation.snapshot,
                    provider_id=provider_id,
                    blocker=str(exc),
                ))
            failure = (
                exc.failure
                if isinstance(exc, ProviderActionError)
                else self.capture_provider_failure(
                    model=PROVIDER_LABELS.get(provider_id, provider_id),
                    action=(
                        "task"
                        if "provider" in locals() and provider is not None
                        else "connect"
                    ),
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
                "provider": provider_id,
                "provider_failure": failure.to_dict() if failure else None,
            })
        finally:
            cancellation.set_event(previous_cancel_event)
            provider_controls.end_task_context()
            try:
                if "provider" in locals() and provider is not None:
                    provider.close()
            except Exception:
                pass

    @staticmethod
    def _ui_event(run_id: str, session_id: str, event: RunEvent) -> dict | None:
        if event.kind == "turn":
            return {"type": "turn", "run_id": run_id, "session_id": session_id, "turn": event.turn}
        if event.kind == "info":
            text = event.message
            names = str(event.metadata.get("names") or "")
            if names:
                text = f"{text}: {names}"
            return {"type": "info", "run_id": run_id, "session_id": session_id, "text": text}
        if event.kind != "tool" or event.call is None or event.outcome is None:
            return None
        path = str(event.call.args.get("path") or "")
        result = event.outcome.first_line(200)
        return {
            "type": "tool",
            "run_id": run_id,
            "session_id": session_id,
            "turn": event.turn,
            "kind": event.call.name,
            "path": "" if path == "." else path,
            "result": result,
            "error": not event.outcome.ok,
        }
