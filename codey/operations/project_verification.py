"""Refresh request-supplied verification hints without treating them as proof."""
from __future__ import annotations

from codey.completion.verification_policy import select_verification_candidate


def refresh_verification_candidates(session) -> None:
    if not session.edited_files or getattr(session, "verification_forbidden", False) is True:
        return
    epoch = max(session.edited_files.values())
    loader = getattr(session, "verification_candidate_loader", None)
    if callable(loader) and session.verification_candidates_epoch != epoch:
        try:
            session.verification_candidates = tuple(loader() or ())
        except (OSError, TypeError, ValueError):
            session.verification_candidates = ()
        session.verification_candidates_epoch = epoch
    candidates = getattr(session, "verification_candidates", ())
    session.selected_verification = (select_verification_candidate(candidates, tuple(session.edited_files))
                                     if candidates else None)
