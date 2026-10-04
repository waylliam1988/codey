"""Explicit review reuse (cold start, strict match only)."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from codey.reviews.core import ReviewResult
from codey.reviews.identity import ReviewIdentity, identities_match
from codey.reviews.input import ReviewScope


def validate_source_run_id(value: object) -> str:
    if value == "":
        return ""
    from codey.reviews.persistence import _safe_component

    return _safe_component(value)


def try_reuse_review(
    *,
    state_home: str | Path,
    session_id: str,
    current_run_id: str,
    current_project: str,
    source_run_id: str,
    current_scope: ReviewScope,
    current_identity: ReviewIdentity,
    current_snapshot_ok: bool,
) -> ReviewResult | None:
    """Return a reused fresh-marked result on exact match, else None.

    Raises ValueError for illegal params (path-like source, self-source is
    treated as miss, not error; unsupported intent is checked by callers).
    """
    source = validate_source_run_id(source_run_id)
    if not source or source == current_run_id:
        return None
    if not current_snapshot_ok or not current_scope.is_complete:
        return None
    if not current_identity.model_id:
        return None
    try:
        from codey.reviews.persistence import load_recorded_review

        recorded = load_recorded_review(state_home, session_id, source)
        if recorded is None:
            return None
        projection, restored = recorded
        if projection.stop_reason != "done" or projection.project != current_project or not restored.is_complete:
            return None
        if not _scope_matches_current(restored, current_scope):
            return None
        source_identity = restored.identity
        if not identities_match(source_identity, current_identity):
            return None
        return replace(
            restored,
            origin="reused",
            identity=source_identity,
            source_run_id=restored.source_run_id or source,
        )
    except (ValueError, OSError):
        return None


def _scope_matches_current(restored, current_scope) -> bool:
    provided = set(current_scope.provided_files)
    for finding in restored.findings:
        if finding.path not in provided:
            return False
    from codey.reviews.identity import scope_digest_for

    return scope_digest_for(restored.scope) == scope_digest_for(current_scope)
