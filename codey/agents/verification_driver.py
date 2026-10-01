"""Project executor accounting; completion decisions belong to the task gate."""
from __future__ import annotations

from codey.agents.state import AgentLoopSession


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


__all__ = ["record_edit_change", "record_run_attempt"]
