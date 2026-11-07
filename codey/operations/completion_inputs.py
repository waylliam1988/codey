"""Immutable collected facts at the completion boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from codey.completion.verification_policy import VerificationCandidate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.verification_observation import VerificationObservation, verification_observations


@dataclass(frozen=True)
class CompletionCheckInput:
    """Extensions inspect values, never the live mutable controller/session."""

    task_kind: str
    task_text: str
    policy: TaskPolicy
    edited_files: Mapping[str, int]
    verifications: tuple[VerificationObservation, ...]
    executed: frozenset[str]

    @classmethod
    def capture(cls, session: TaskSession) -> CompletionCheckInput:
        return cls(session.task_kind, session.task_text, session.policy,
                   MappingProxyType(dict(session.edited_files)),
                   verification_observations(session.verifications), frozenset(session.executed))


@dataclass(frozen=True)
class ProjectCompletionFacts:
    policy: TaskPolicy
    project_changes_required: bool
    verification_forbidden: bool
    selected_verification: VerificationCandidate | None
    verification_candidates_refresh_failed: bool
    workspace_revision: int
    workspace_fingerprint: str
    edited_files: Mapping[str, int]
    verifications: tuple[VerificationObservation, ...]

    @classmethod
    def capture(cls, session: TaskSession) -> ProjectCompletionFacts:
        return cls(session.policy, session.project_changes_required, session.verification_forbidden,
                   session.selected_verification, session.verification_candidates_refresh_failed,
                   session.workspace_revision, session.workspace_fingerprint,
                   MappingProxyType(dict(session.edited_files)), verification_observations(session.verifications))
