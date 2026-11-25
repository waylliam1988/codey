"""Compose domain handoffs before freezing the provider request."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from codey.durable.model import thaw_json
from codey.types.json_value import decode_json_object

if TYPE_CHECKING:
    from codey.durable.session import DurableSession
    from codey.operations.completion_context import CompletionContext
    from codey.operations.kernel.kernel_protocol import TurnSnapshot
    from codey.operations.kernel_contracts import KernelExecutionDeps
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolResult
    from codey.storage.managed_outputs import ManagedOutputStore


def prepare_request_snapshot(
    session: TaskSession, snapshot: TurnSnapshot, execution: KernelExecutionDeps, completion: CompletionContext,
    *, durable: DurableSession, run_id: str, managed_outputs: ManagedOutputStore,
    collect_memory: bool, delivered_results: Sequence[ToolResult],
) -> tuple[TurnSnapshot, str]:
    """Finish tool narrowing before rendering results or request options."""
    if collect_memory:
        from codey.operations.memory_candidate_transport import offer_memory_candidates

        snapshot = offer_memory_candidates(snapshot)
    browser = execution.browser_session
    from codey.operations.browser.browser_resources import resume_assistance_observation
    from codey.operations.task_handoff import task_handoff
    from codey.operations.tool_task_delivery import active_tool_task_controls
    from codey.operations.verification_workflow import verification_workflow

    question = decode_json_object(thaw_json(durable.task(run_id).state.get("question", {})))
    assistance = (resume_assistance_observation(browser, question.get("continuation", {}), session._memory_results)
                  if question.get("state") == "answered" else "")
    snapshot, workflow = verification_workflow(session, snapshot, durable, run_id,
        managed_outputs, execution.workspace_revision_store,
        ignored_paths=execution.workspace_ignored_paths)
    snapshot, hint = task_handoff(session, snapshot, completion, browser,
                                 delivered_results=delivered_results,
                                 task_controls=active_tool_task_controls(durable, run_id))
    from codey.operations.project.edit_base import edit_anchor_retry

    snapshot, edit_retry = edit_anchor_retry(session, snapshot, tuple(delivered_results))
    instructions = ""
    if execution.project_path is not None and "edit" in snapshot.inventory_names:
        from codey.operations.project.project_delivery import declared_edit_target
        from codey.operations.project.project_instructions import instruction_handoff

        targets = session.policy.edit_scope or tuple(filter(None, (declared_edit_target(session.task_text),)))
        bindings, instructions = instruction_handoff(
            execution.project_path, (*targets, *session.read_files), session._memory_results)
        snapshot = replace(snapshot, project_instructions=bindings)
    return snapshot, "\n\n".join(part for part in (assistance, instructions, edit_retry, hint, workflow) if part)
