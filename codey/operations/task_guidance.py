"""Select domain guidance at task composition, before entering the common loop."""

from codey.policies.task_policy import TaskPolicy
from codey.research.completion_guidance import render_completion_guidance


def task_guidance_for_policy(policy: TaskPolicy) -> str:
    return render_completion_guidance(policy) if policy.strict_research else ""
