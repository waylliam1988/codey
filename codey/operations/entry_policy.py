from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from codey.task.model import TaskSubmission

if TYPE_CHECKING:
    from codey.policies.task_policy import TaskPolicy


_TASK_KINDS = frozenset({"project", "research", "hybrid", "planning", "planning_readonly", "readonly"})


def _task_kind(task_kind: object) -> str:
    kind = str(task_kind or "").strip().lower()
    if kind in _TASK_KINDS:
        return "planning" if kind in {"planning_readonly", "readonly"} else kind
    return "chat" if kind == "auto" else kind


def build_task_policy_for_entry(request: TaskSubmission, task_kind: object) -> TaskPolicy:
    from codey.agents.protocol import task_forbids_verification
    from codey.policies.task_policy import policy_for_dispatch

    kind = _task_kind(task_kind)
    strict = kind == "research" or request.strict_research is True
    policy = policy_for_dispatch(request, kind, strict_research=strict)
    if policy is None:
        raise RuntimeError("task policy configuration failed")
    if task_forbids_verification(request.task):
        policy = replace(policy, grants=policy.grants - {"project.verify"},
                         denied_capabilities=policy.denied_capabilities | {"project.verify"},
                         source=policy.source + ";verification:forbidden")
    if request.project and policy.allows("project.write"):
        from codey.operations.project.project_delivery import declared_edit_scope, declared_project_checks

        checks = declared_project_checks(request.task)
        if checks:
            policy = replace(policy, required_checks=tuple(dict.fromkeys((*policy.required_checks, *checks))))
        if edit_scope := declared_edit_scope(Path(request.project), request.task):
            policy = replace(policy, edit_scope=edit_scope)
    return policy
