"""Select domain guidance at task composition, before entering the common loop."""

from typing import TYPE_CHECKING

from codey.policies.output_requirement import guidance
from codey.policies.task_policy import TaskPolicy
from codey.research.completion_guidance import render_completion_guidance

if TYPE_CHECKING:
    from codey.operations.task_session import TaskSession


def task_working_context(session: "TaskSession") -> str:
    from codey.operations.kernel_prompt import working_context
    from codey.research.completion_guidance import render_research_observations

    # Pure research has no workspace mutations to verify. Its task already
    # appears verbatim in the admitted prompt; repeating it here also repeats
    # parent reports/plans in investigations and obscures the latest evidence.
    research_only = (session.task_kind in {"research", "evidence_followup"}
                     and not session.policy.allows("project.write"))
    parts = [] if research_only else [working_context(session)]
    if session.policy.strict_research or session.task_kind == "evidence_followup":
        parts.append(render_research_observations(session))
    return "\n\n".join(parts)


def task_guidance_for_policy(policy: TaskPolicy) -> str:
    return "\n".join(part for part in (
        render_completion_guidance(policy) if policy.strict_research else "",
        ("Knowledge actions: when the user requests saving or revising knowledge, execute knowledge_write or "
         "knowledge_commit and wait for success before calling done. Tool JSON inside a code fence or a report "
         "is an example, not a saved change. Keep candidate analogies distinct from verified findings."
         if policy.allows("knowledge.write") else ""),
        ("Use knowledge_read on the relevant versions before revising or adopting them; "
         "copy their actual ids and revisions. If background recall lacks the requested topic or object kind, "
         "search for that target before editing. A related premise is not the inference being revised."
         if policy.allows("knowledge.read") and policy.allows("knowledge.write") else ""),
        guidance(policy.required_checks),
    ) if part)
