"""Select domain guidance at task composition, before entering the common loop."""

from codey.policies.output_requirement import guidance
from codey.policies.task_policy import TaskPolicy
from codey.research.completion_guidance import render_completion_guidance


def task_guidance_for_policy(policy: TaskPolicy) -> str:
    return "\n".join(part for part in (
        render_completion_guidance(policy) if policy.strict_research else "",
        guidance(policy.required_checks),
    ) if part)
