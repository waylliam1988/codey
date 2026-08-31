"""Verification state helpers for the coding-agent loop."""

from __future__ import annotations

from codey.agents.protocol import (
    default_verification_reminder,
    task_forbids_verification,
    task_requests_verification,
    verification_reminder,
)
from codey.agents.request import AgentRequest
from codey.agents.state import AgentLoopSession, LoopVerification
from codey.completion.verification_policy import (
    VerificationCandidate,
    check_covers_selected_candidate,
    select_verification_candidate,
)


def initial_verification_state(request: AgentRequest) -> LoopVerification:
    return LoopVerification(
        paths=set(request.verification_changed_files),
        edit_epoch=0,
        successful_checks=[
            (item.command, item.cwd, 0)
            for item in request.verification_successful_checks
        ],
        attempts=[],
    )


def requires_verification(task: str) -> bool:
    return task_requests_verification(task)


def forbids_verification(task: str) -> bool:
    return task_forbids_verification(task)


def requested_verification_reminder(session: AgentLoopSession) -> str:
    return verification_reminder(session.user_task)


def default_candidate_reminder(candidate: VerificationCandidate) -> str:
    return default_verification_reminder(candidate)


def ensure_verification_candidates(
    session: AgentLoopSession,
) -> tuple[VerificationCandidate, ...]:
    if (
        session.verification_candidate_loader is not None
        and session.verification.paths
        and session.verification.candidates_epoch != session.verification.edit_epoch
    ):
        try:
            session.verification_candidates = session.verification_candidate_loader()
        except (OSError, TypeError, ValueError):
            session.verification_candidates = ()
        session.verification.candidates_epoch = session.verification.edit_epoch
    return session.verification_candidates


def selected_verification_candidate(
    session: AgentLoopSession,
) -> VerificationCandidate | None:
    if not session.verification.paths:
        return None
    return select_verification_candidate(
        ensure_verification_candidates(session),
        tuple(session.verification.paths),
    )


def verification_is_fresh(
    session: AgentLoopSession,
    candidate: VerificationCandidate | None,
) -> bool:
    return candidate is not None and any(
        epoch == session.verification.edit_epoch
        and check_covers_selected_candidate(
            candidate,
            command,
            cwd,
            tuple(session.verification.paths),
            root=session.project,
        )
        for command, cwd, epoch in session.verification.successful_checks
    )


def verification_attempted_after_latest_edit(session: AgentLoopSession) -> bool:
    return any(
        epoch == session.verification.edit_epoch
        for _command, _cwd, epoch in session.verification.attempts
    ) or any(
        epoch == session.verification.edit_epoch
        for _command, _cwd, epoch in session.verification.successful_checks
    )


def mark_policy_denied_run(session: AgentLoopSession) -> None:
    session.verification.checks_ran = True
    session.verification.checks_passed = False


def record_edit_change(session: AgentLoopSession, canonical_path: str) -> None:
    session.progress.wrote_files = True
    session.verification.checks_passed = False
    session.progress.changed_files.add(canonical_path)
    session.verification.paths.add(canonical_path)
    session.progress.known_file_paths.add(canonical_path)
    session.verification.edit_epoch += 1


def record_run_attempt(
    session: AgentLoopSession,
    *,
    command: str,
    path: str,
    ok: bool,
) -> None:
    session.verification.checks_ran = True
    session.verification.attempts.append(
        (command, path, session.verification.edit_epoch)
    )
    session.verification.checks_passed = ok
    if ok:
        session.verification.successful_checks.append(
            (command, path, session.verification.edit_epoch)
        )


__all__ = [
    "default_candidate_reminder",
    "ensure_verification_candidates",
    "forbids_verification",
    "initial_verification_state",
    "mark_policy_denied_run",
    "record_edit_change",
    "record_run_attempt",
    "requested_verification_reminder",
    "requires_verification",
    "selected_verification_candidate",
    "verification_attempted_after_latest_edit",
    "verification_is_fresh",
]
