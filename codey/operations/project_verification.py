"""Refresh request-supplied verification hints without treating them as proof."""
from __future__ import annotations

from typing import Any

from codey.completion.verification_policy import select_verification_candidate


def _validate_candidates(rows: object) -> tuple[Any, ...]:
    from codey.completion.verification_policy import VerificationCandidate

    if rows is None:
        raise ValueError("verification candidates loader returned None")
    if not isinstance(rows, (list, tuple)):
        raise ValueError("verification candidates must be a list or tuple")
    cleaned: list[Any] = []
    for item in rows:
        if not isinstance(item, VerificationCandidate):
            raise ValueError("verification candidate must be a VerificationCandidate")
        if not str(getattr(item, "command", "") or "").strip():
            raise ValueError("verification candidate command is empty")
        if not isinstance(getattr(item, "cwd", None), str):
            raise ValueError("verification candidate cwd must be a string")
        cleaned.append(item)
    return tuple(cleaned)


def refresh_verification_candidates(session: Any) -> None:
    if not session.edited_files or getattr(session, "verification_forbidden", False) is True:
        return
    epoch = max(session.edited_files.values())
    loader = getattr(session, "verification_candidate_loader", None)
    if callable(loader) and session.verification_candidates_epoch != epoch:
        import contextlib

        try:
            fresh = _validate_candidates(loader())
        except Exception:
            # Preserve the known requirement; never clear to empty on
            # failure and never mark the epoch as successfully refreshed.
            # A legal empty return is distinct: it succeeds and clears.
            # None, illegal members, and any loader/validation error all
            # fail closed here.
            with contextlib.suppress(Exception):
                session.verification_candidates_refresh_failed = True
            return
        session.verification_candidates = fresh
        session.verification_candidates_epoch = epoch
        with contextlib.suppress(Exception):
            session.verification_candidates_refresh_failed = False
    candidates = getattr(session, "verification_candidates", ())
    session.selected_verification = (select_verification_candidate(candidates, tuple(session.edited_files))
                                     if candidates else None)
