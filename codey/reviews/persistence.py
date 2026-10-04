"""Bounded review artifact persistence (cold start, no compat)."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from codey.reviews.core import REVIEW_CONTRACT_VERSION, ReviewResult

SCHEMA_VERSION = 1
MAX_ARTIFACT_BYTES = 64 * 1024
MAX_FINDINGS_PERSISTED = 8


@dataclass(frozen=True)
class ReviewArtifactRef:
    session_id: str
    run_id: str
    attempt_id: str
    path: Path
    sha256: str
    finding_count: int


def append_review_result_ledger(
    append_ledger: Callable[[Callable[[object], None]], None] | None,
    review: ReviewResult | None,
) -> None:
    """Persist the bounded review result through the caller's run ledger."""

    if append_ledger is None or review is None:
        return
    identity = getattr(review, "identity", None)
    attempt_id = str(getattr(identity, "attempt_id", "") or "")
    if not attempt_id:
        return
    verdict = str(getattr(review, "verdict", "") or "")[:40]
    status = str(getattr(review, "status", "") or "")[:40]
    origin = str(getattr(review, "origin", "fresh") or "fresh")[:40]
    try:
        finding_count = len(getattr(review, "findings", ()) or ())
    except Exception:
        finding_count = 0
    artifact_sha = str(getattr(identity, "artifact_sha256", "") or "")[:80]
    source_run_id = str(getattr(review, "source_run_id", "") or "")[:120]

    def _append(writer: object, event_type: str) -> None:
        append = getattr(writer, "append", None)
        if not callable(append):
            return
        append(
            event_type,
            review_attempt_id=attempt_id[:80],
            verdict=verdict,
            status=status,
            origin=origin,
            finding_count=max(0, finding_count),
            artifact_sha256=artifact_sha or None,
            source_review_run_id=source_run_id or None,
        )

    append_ledger(lambda writer: _append(writer, "review_result_projected"))
    append_ledger(lambda writer: _append(writer, "review_finished"))


class ReviewArtifactStore:
    def __init__(self, state_home: str | Path) -> None:
        if not state_home:
            raise ValueError("state_home required")
        self.root = Path(state_home) / "review_artifacts"

    def path_for(self, session_id: str, run_id: str, attempt_id: str) -> Path:
        from codey.storage.local_store import session_key as _session_key

        safe_session = _session_key(session_id)
        safe_run = _safe_component(run_id)
        safe_attempt = _safe_component(attempt_id)
        path = self.root / safe_session / safe_run / f"{safe_attempt}.json"
        resolved_root = self.root.resolve()
        # Resolve even a not-yet-created file so an existing symlinked parent
        # cannot redirect a later mkdir/write outside the artifact store.
        resolved = path.resolve()
        try:
            resolved.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("review artifact path escapes store") from exc
        return resolved


def save_review_artifact(
    store: ReviewArtifactStore,
    *,
    session_id: str,
    run_id: str,
    attempt_id: str,
    result: ReviewResult,
    scope_digest: str,
    prompt_digest: str,
    snapshot_digest: str,
    reviewer_id: str,
    policy: str,
    permission_profile: str = "reviewer",
    model_id: str = "",
    self_review: bool = False,
) -> ReviewArtifactRef | None:
    from codey.policies.action import ActionSubject, evaluate_action
    from codey.storage.atomic_io import write_bytes_atomic

    payload = _artifact_payload(
        session_id=session_id,
        run_id=run_id,
        attempt_id=attempt_id,
        result=result,
        scope_digest=scope_digest,
        prompt_digest=prompt_digest,
        snapshot_digest=snapshot_digest,
        reviewer_id=reviewer_id,
        policy=policy,
        model_id=model_id,
        self_review=self_review,
    )
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = raw.encode("utf-8")
    if len(encoded) > MAX_ARTIFACT_BYTES:
        return None
    policy_decision = evaluate_action(ActionSubject(
        kind="review_output",
        phase="review_persist",
        permission_profile=permission_profile,
        byte_count=len(encoded),
        item_count=0,
    ))
    from codey.policies.action import DECISION_DENY

    if policy_decision.decision == DECISION_DENY:
        return None
    try:
        path = store.path_for(session_id, run_id, attempt_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(path, encoded)
        verified = load_review_artifact(store, session_id=session_id, run_id=run_id, attempt_id=attempt_id)
        _ = verified
    except (OSError, ValueError, TypeError):
        return None
    digest = hashlib.sha256(encoded).hexdigest()
    return ReviewArtifactRef(
        session_id=session_id,
        run_id=run_id,
        attempt_id=attempt_id,
        path=path,
        sha256=digest,
        finding_count=len(result.findings),
    )


def load_review_artifact(
    store: ReviewArtifactStore,
    *,
    session_id: str,
    run_id: str,
    attempt_id: str,
) -> ReviewResult:
    from codey.reviews.input import ReviewScope

    path = store.path_for(session_id, run_id, attempt_id)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"review artifact missing: {exc}") from exc
    if len(raw) > MAX_ARTIFACT_BYTES + 1024:
        raise ValueError("review artifact exceeds size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"review artifact unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("review artifact invalid")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("review artifact schema mismatch")
    if payload.get("session_id") != session_id or payload.get("run_id") != run_id:
        raise ValueError("review artifact identity mismatch")
    if payload.get("contract_version") != REVIEW_CONTRACT_VERSION:
        raise ValueError("review artifact contract mismatch")
    findings = []
    from codey.reviews.core import ReviewFinding

    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        raise ValueError("review artifact findings invalid")
    if len(raw_findings) > MAX_FINDINGS_PERSISTED:
        raise ValueError("review artifact findings exceed limit")
    for item in raw_findings:
        if not isinstance(item, dict):
            raise ValueError("review artifact finding invalid")
        finding_id = item.get("finding_id")
        path_value = item.get("path")
        issue = item.get("issue")
        if not isinstance(finding_id, str) or not finding_id:
            raise ValueError("review artifact finding_id invalid")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError("review artifact path invalid")
        if not isinstance(issue, str) or not issue:
            raise ValueError("review artifact issue invalid")
        raw_fix = item.get("suggested_fix")
        fix = raw_fix if isinstance(raw_fix, str) else ""
        raw_hunk = item.get("hunk_index")
        hunk = raw_hunk if type(raw_hunk) is int else None
        raw_new = item.get("new_line")
        new_line = raw_new if type(raw_new) is int else None
        raw_old = item.get("old_line")
        old_line = raw_old if type(raw_old) is int else None
        findings.append(ReviewFinding(path_value, issue, fix, hunk, new_line, old_line))
    scope_payload = payload.get("scope") or {}
    scope = ReviewScope(
        total_changed_files=int(scope_payload.get("total_changed_files") or 0),
        provided_files=tuple(scope_payload.get("provided_files") or ()),
        excluded_files=tuple(scope_payload.get("excluded_files") or ()),
        diff_truncated=bool(scope_payload.get("diff_truncated")),
        file_list_truncated=bool(scope_payload.get("file_list_truncated")),
        context_truncated=bool(scope_payload.get("context_truncated")),
        collection_incomplete=bool(scope_payload.get("collection_incomplete")),
    )
    return ReviewResult(
        verdict=str(payload.get("verdict") or "unknown"),
        summary=str(payload.get("summary") or ""),
        findings=findings,
        status=str(payload.get("status") or "incomplete"),
        origin="reused",
        diagnostics=tuple(payload.get("diagnostics") or ()),
        scope=scope,
    )


def review_trace_payload(result: ReviewResult, *, scope_digest: str, prompt_digest: str) -> dict[str, object]:
    return {
        "verdict": result.verdict,
        "status": result.status,
        "origin": result.origin,
        "finding_count": len(result.findings),
        "scope_digest": scope_digest[:16],
        "prompt_digest": prompt_digest[:16],
        "diagnostic_count": len(result.diagnostics),
    }


def _artifact_payload(
    *,
    session_id: str,
    run_id: str,
    attempt_id: str,
    result: ReviewResult,
    scope_digest: str,
    prompt_digest: str,
    snapshot_digest: str,
    reviewer_id: str,
    policy: str,
    model_id: str = "",
    self_review: bool = False,
) -> dict[str, object]:
    findings = []
    for index, finding in enumerate(list(result.findings)[:MAX_FINDINGS_PERSISTED]):
        stable = f"{finding.path}\x00{finding.issue}\x00{finding.suggested_fix}\x00{finding.hunk_index}\x00{finding.new_line}\x00{finding.old_line}"
        finding_id = "review_finding:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]
        findings.append({
            "finding_id": finding_id,
            "index": index,
            "path": finding.path,
            "issue": finding.issue,
            "suggested_fix": finding.suggested_fix,
            "hunk_index": finding.hunk_index,
            "new_line": finding.new_line,
            "old_line": finding.old_line,
        })
    scope = getattr(result, "scope", None)
    scope_payload = {
        "total_changed_files": int(getattr(scope, "total_changed_files", 0) or 0),
        "provided_files": list(getattr(scope, "provided_files", ()) or ()),
        "excluded_files": list(getattr(scope, "excluded_files", ()) or ()),
        "diff_truncated": bool(getattr(scope, "diff_truncated", False)),
        "file_list_truncated": bool(getattr(scope, "file_list_truncated", False)),
        "context_truncated": bool(getattr(scope, "context_truncated", False)),
        "collection_incomplete": bool(getattr(scope, "collection_incomplete", False)),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": REVIEW_CONTRACT_VERSION,
        "session_id": session_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "reviewer_id": reviewer_id,
        "policy": policy,
        "model_id": model_id,
        "self_review": bool(self_review),
        "scope_digest": scope_digest,
        "prompt_digest": prompt_digest,
        "snapshot_digest": snapshot_digest,
        "verdict": result.verdict,
        "status": result.status,
        "summary": result.summary[:500],
        "findings": findings,
        "diagnostics": list(result.diagnostics)[:8],
        "scope": scope_payload,
    }


def _safe_component(value: object) -> str:
    import re as _re

    text = _re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())[:120].strip("._")
    if not text:
        raise ValueError("invalid artifact component")
    return text
