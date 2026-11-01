"""Prepare an unsent kernel turn once; received turns reuse their original input."""
from dataclasses import dataclass
from typing import Any

from codey.operations import kernel_prompt, kernel_protocol
from codey.operations.kernel_protocol import TurnSnapshot
from codey.operations.project_prompt_context import prepare_coding_context
from codey.operations.task_session import TaskSession
from codey.policies.tool_requirement import parse_tool_success_check


@dataclass(frozen=True)
class PreparedKernelTurn:
    snapshot: TurnSnapshot
    prompt: str


def prepare_kernel_turn(session: TaskSession, *, user_task: str, context_text: str,
                        task_guidance: str, native: bool, completion_context: Any = None,
                        request_scoped_contract: bool = False) -> PreparedKernelTurn:
    snapshot = kernel_protocol.build_turn_snapshot(session, native=native)
    from codey.research.plan_contract import is_compact_plan_spec

    compact_planning = any(spec.name == "research_plan" and is_compact_plan_spec(spec) for spec in snapshot.model_specs)
    if compact_planning:
        from codey.research.completion_guidance import render_completion_guidance

        task_guidance = task_guidance.replace(render_completion_guidance(snapshot.policy),
                                             render_completion_guidance(snapshot.policy, planning=True))
    if snapshot.research_scope is not None:
        from codey.research.completion_guidance import render_completion_guidance

        task_guidance = task_guidance.replace(render_completion_guidance(snapshot.policy),
                                             render_completion_guidance(snapshot.policy, local=True,
                                                 submission_mode=snapshot.research_scope.submission_mode))
    guidance = [task_guidance] if task_guidance else []
    if snapshot.policy.strict_research and snapshot.research_scope is None:
        from codey.research.methods import research_method_guidance

        guidance.append(research_method_guidance(user_task, evidence_only=session.task_kind == "evidence_followup",
                                                   finding_report=session.task_kind == "research", compact_planning=compact_planning))
    for check_id in snapshot.policy.required_checks:
        requirement = parse_tool_success_check(check_id)
        if requirement is not None:
            guidance.append(requirement.guidance)
    prompt = kernel_prompt.kernel_prompt_for_session(
        session, user_task=user_task, contract_text=snapshot.contract_text,
        context_text=context_text,
        native=native, tool_names=snapshot.tool_names, task_guidance="\n\n".join(guidance),
        coding_context=prepare_coding_context(session, completion_context=completion_context),
        request_scoped_contract=request_scoped_contract,
        done_spec=next((spec for spec in snapshot.model_specs if spec.name == "done"), None),
        local_submission=snapshot.research_scope is not None,
        research_planning=compact_planning,
    )
    return PreparedKernelTurn(snapshot, prompt)
