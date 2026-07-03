"""Task orchestration independent of HTTP request handling."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from codey import cancellation, provider_controls
from codey.agent import RunResult
from codey.events import RunEvent, render_run_event
from codey.handoff import ConversationSnapshot, render_continuation_prompt
from codey.project_facts import ProjectFactsStore
from codey.providers import PROVIDER_LABELS
from codey.receipt import build_task_receipt
from codey.review import has_reviewable_changes, render_writer_followup


@dataclass(frozen=True)
class TaskRequest:
    session_id: str
    project: str | None
    task: str
    max_turns: int
    continue_task: bool
    provider_id: str
    run_id: str = ""


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
        project_facts: ProjectFactsStore | None = None,
        is_git_repository: Callable[[str | Path], bool] | None = None,
        review_fix_turns: int = 12,
        review_log_lines: int = 80,
    ) -> None:
        self.state = state
        self.agent_run = agent_run
        self.collect_changes = collect_changes
        self.run_review = run_review
        self.capture_provider_failure = capture_provider_failure
        self.project_facts = project_facts
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

        def on_event(event: RunEvent) -> None:
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
                try:
                    self.project_facts.record_success(
                        project,
                        str(event.call.args.get("path") or "."),
                        str(event.call.args.get("command") or ""),
                    )
                except (OSError, ValueError):
                    pass

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
            provider = state.get_provider(provider_id)
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
            if project:
                verified_facts = (
                    self.project_facts.render(project)
                    if self.project_facts is not None
                    else ""
                )
                key = str(Path(project).expanduser().resolve())
                tracker = state.change_tracker_for(
                    key,
                    persistent=not self.is_git_repository(key),
                )
                result = self.agent_run(
                    provider,
                    Path(project),
                    task,
                    max_turns=max_turns,
                    on_event=on_event,
                    on_shell_request=on_shell_request,
                    stop_flag=state.stop_flag,
                    fresh_chat=fresh_chat,
                    change_tracker=tracker,
                    conversation=conversation,
                    provider_id=provider_id,
                    handoff=handoff,
                    project_facts=verified_facts,
                )
                state.set_provider_session(
                    provider_id,
                    None if result.stop_reason == "stopped" else session_id,
                )
                if result.stop_reason == "done" and not state.stop_flag.is_set():
                    task_changes = self.collect_changes(project, tracker)
                    if has_reviewable_changes(task_changes):
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
                                writer_id=provider_id,
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
                                followup = render_writer_followup(task, review)
                                provider = state.get_provider(provider_id)
                                verified_facts = (
                                    self.project_facts.render(project)
                                    if self.project_facts is not None
                                    else ""
                                )
                                result = self.agent_run(
                                    provider,
                                    Path(project),
                                    followup,
                                    max_turns=min(max_turns, self.review_fix_turns),
                                    on_event=on_event,
                                    on_shell_request=on_shell_request,
                                    stop_flag=state.stop_flag,
                                    fresh_chat=False,
                                    change_tracker=tracker,
                                    conversation=conversation,
                                    provider_id=provider_id,
                                    project_facts=verified_facts,
                                )
            else:
                if fresh_chat:
                    provider.new_chat()
                prompt = render_continuation_prompt(handoff, task) if handoff else task
                reply = provider.send(prompt)
                if fresh_chat:
                    conversation.begin_window(provider_id, "chat")
                state.set_provider_session(provider_id, session_id)
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
                task_changes = self.collect_changes(project, tracker)
                receipt = build_task_receipt(
                    task_changes,
                    checks_passed=result.checks_passed,
                )
                files = tuple(
                    str(item.get("path") or "")
                    for item in (task_changes.get("files") or [])
                    if item.get("path")
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
                event["receipt"] = receipt.to_dict()
                if task_changes and task_changes.get("ok"):
                    event["changes"] = {
                        "changed_count": task_changes.get("changed_count", 0),
                        "files": task_changes.get("files", [])[:3],
                        "mode": task_changes.get("mode"),
                        "project": project,
                    }
            state.finish_run(run_id, event)
        except (provider_controls.ControlTeachCancelled, cancellation.TaskCancelled):
            state.set_provider_session(provider_id, None)
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
            if "conversation" in locals():
                conversation.update_snapshot(replace(
                    conversation.snapshot,
                    provider_id=provider_id,
                    blocker=str(exc),
                ))
            if "provider" in locals() and provider is not None:
                failure = getattr(provider, "last_failure", None)
            else:
                failure = self.capture_provider_failure(
                    model=PROVIDER_LABELS.get(provider_id, provider_id),
                    action="connect",
                    page=None,
                    error=exc,
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
