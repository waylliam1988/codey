"""Explicit review reuse (cold start, strict match only)."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from codey.reviews.core import ReviewResult
from codey.reviews.identity import ReviewIdentity, identities_match
from codey.reviews.input import ReviewScope


def validate_source_run_id(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "/" in text or "\\" in text or ".." in text:
        raise ValueError("review source must be a run id, not a path")
    if len(text) > 120:
        raise ValueError("review source run id too long")
    return text


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
        projection = _source_projection(state_home, session_id, source, current_project)
        if projection is None:
            return None
        attempt_id = str(getattr(projection, "attempt_id", "") or "")
        if not attempt_id:
            return None
        restored = _load_source_artifact(state_home, session_id, source, attempt_id)
        if restored is None:
            return None
        if not _scope_matches_current(restored, current_scope):
            return None
        source_identity = _source_identity(state_home, session_id, source, attempt_id, current_project)
        if source_identity is None:
            return None
        if not identities_match(source_identity, current_identity):
            return None
        return replace(restored, origin="reused")
    except (ValueError, OSError):
        return None


def _source_projection(state_home, session_id, source, current_project):
    from codey.runs.ledger import RunLedgerStore
    from codey.runs.ledger_projection import load_run_projection

    projection = load_run_projection(RunLedgerStore(state_home), session_id, source)
    if projection is None or not projection.complete:
        return None
    if projection.project and current_project and projection.project != current_project:
        return None
    review_summary = getattr(projection, "review", None)
    if review_summary is None:
        return None
    if str(getattr(review_summary, "status", "") or "") != "complete":
        return None
    return review_summary


def _load_source_artifact(state_home, session_id, source, attempt_id):
    from codey.reviews.persistence import ReviewArtifactStore, load_review_artifact

    try:
        restored = load_review_artifact(
            ReviewArtifactStore(state_home), session_id=session_id, run_id=source, attempt_id=attempt_id
        )
    except (ValueError, OSError):
        return None
    if restored.status != "complete":
        return None
    if restored.scope is not None and not restored.scope.is_complete:
        return None
    return restored


def _scope_matches_current(restored, current_scope) -> bool:
    provided = set(current_scope.provided_files)
    for finding in restored.findings:
        if finding.path not in provided:
            return False
    from codey.reviews.identity import scope_digest_for

    return scope_digest_for(restored.scope) == scope_digest_for(current_scope)


def _source_identity(state_home, session_id, source, attempt_id, current_project):
    import json as _json

    from codey.reviews.core import REVIEW_CONTRACT_VERSION

    try:
        from codey.reviews.persistence import ReviewArtifactStore

        raw = ReviewArtifactStore(state_home).path_for(session_id, source, attempt_id).read_bytes()
        payload = _json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return ReviewIdentity(
            scope_digest=str(payload.get("scope_digest") or ""),
            prompt_digest=str(payload.get("prompt_digest") or ""),
            snapshot_digest=str(payload.get("snapshot_digest") or ""),
            project=current_project,
            reviewer_id=str(payload.get("reviewer_id") or ""),
            model_id=str(payload.get("model_id") or ""),
            contract_version=int(payload.get("contract_version") or REVIEW_CONTRACT_VERSION),
            policy=str(payload.get("policy") or ""),
            self_review=bool(payload.get("self_review", False)),
        )
    except (TypeError, ValueError):
        return None
