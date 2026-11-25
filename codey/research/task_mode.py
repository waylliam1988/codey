"""Shared task-mode predicates for research contracts and question binding."""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from codey.policies.task_policy import TaskPolicy


class ResearchPolicyView(Protocol):
    @property
    def task_kind(self) -> str: ...

    @property
    def policy(self) -> TaskPolicy: ...


def whole_research(session: ResearchPolicyView) -> bool:
    return whole_research_policy(session.policy, task_kind=session.task_kind)


def whole_research_policy(policy: TaskPolicy, *, task_kind: str) -> bool:
    return policy.strict_research and task_kind != "evidence_followup"
