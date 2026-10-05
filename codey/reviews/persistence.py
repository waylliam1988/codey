"""Bounded review artifact persistence (cold start, no compat)."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    expected_sha256: str = "",
    project: str = "",
) -> ReviewResult:
    """Validate and reconstruct one bounded read, including its exact identity."""
    from codey.reviews.identity import ReviewIdentity

    path = store.path_for(session_id, run_id, attempt_id)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
    except OSError as exc:
        raise ValueError("review artifact missing") from exc
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError("review artifact exceeds size limit")
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise ValueError("review artifact digest mismatch")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("review artifact unreadable") from exc
    if not isinstance(payload, dict):
        raise ValueError("review artifact invalid")
    _validate_artifact_contract(payload, session_id, run_id, attempt_id)
    scope = _read_scope(payload.get("scope"))
    findings = _read_findings(payload.get("findings"), scope)
    diagnostics = payload.get("diagnostics")
    if not isinstance(diagnostics, list) or len(diagnostics) > 8 or any(not isinstance(row, str) for row in diagnostics):
        raise ValueError("review artifact diagnostics invalid")
    if payload["verdict"] == "approved" and findings:
        raise ValueError("review artifact verdict conflicts with findings")
    identity = ReviewIdentity(
        scope_digest=payload["scope_digest"], prompt_digest=payload["prompt_digest"],
        snapshot_digest=payload["snapshot_digest"], project=project, reviewer_id=payload["reviewer_id"],
        model_id=payload["model_id"], contract_version=payload["contract_version"], policy=payload["policy"],
        self_review=payload["self_review"], attempt_id=attempt_id, artifact_sha256=digest,
    )
    return ReviewResult(payload["verdict"], payload["summary"], findings,
                        status=payload["status"], origin="fresh", diagnostics=tuple(diagnostics),
                        scope=scope, identity=identity)


def _validate_artifact_contract(payload: Any, session_id: Any, run_id: Any, attempt_id: Any) -> None:
    for name, expected in (("schema_version", SCHEMA_VERSION), ("contract_version", REVIEW_CONTRACT_VERSION)):
        if type(payload.get(name)) is not int or payload[name] != expected:
            raise ValueError("review artifact version mismatch")
    for name, expected_id in (("session_id", session_id), ("run_id", run_id), ("attempt_id", attempt_id)):
        if payload.get(name) != expected_id:
            raise ValueError("review artifact identity mismatch")
    if not isinstance(payload.get("verdict"), str) or payload["verdict"] not in {"approved", "changes_requested", "unknown"}:
        raise ValueError("review artifact verdict invalid")
    if not isinstance(payload.get("status"), str) or payload["status"] not in {"complete", "incomplete", "unavailable", "stale"}:
        raise ValueError("review artifact status invalid")
    if type(payload.get("self_review")) is not bool:
        raise ValueError("review artifact self_review invalid")
    for name in ("summary", "scope_digest", "prompt_digest", "snapshot_digest", "reviewer_id", "model_id", "policy"):
        if not isinstance(payload.get(name), str):
            raise ValueError("review artifact text invalid")


def _read_findings(raw_findings: Any, scope: Any) -> list[Any]:
    from codey.reviews.core import ReviewFinding

    if not isinstance(raw_findings, list) or len(raw_findings) > MAX_FINDINGS_PERSISTED:
        raise ValueError("review artifact findings invalid")
    findings = []
    for item in raw_findings:
        if not isinstance(item, dict):
            raise ValueError("review artifact finding invalid")
        for name in ("finding_id", "path", "issue", "suggested_fix"):
            if not isinstance(item.get(name), str):
                raise ValueError("review artifact finding text invalid")
        if not item["issue"] or item["path"] not in scope.provided_files:
            raise ValueError("review artifact finding outside scope")
        anchors = [item.get(name) for name in ("hunk_index", "new_line", "old_line")]
        if any(value is not None and (type(value) is not int or value <= 0) for value in anchors):
            raise ValueError("review artifact anchor invalid")
        finding = ReviewFinding(item["path"], item["issue"], item["suggested_fix"], *anchors)
        if item["finding_id"] != _finding_id(finding):
            raise ValueError("review artifact finding identity mismatch")
        findings.append(finding)
    return findings


def _unique_object(pairs: Any) -> dict[Any, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate artifact key")
        result[key] = value
    return result


def _read_scope(payload: Any) -> Any:
    from codey.reviews.input import ReviewScope

    if not isinstance(payload, dict):
        raise ValueError("review artifact scope invalid")
    total = payload.get("total_changed_files")
    if type(total) is not int or total < 0:
        raise ValueError("review artifact scope count invalid")
    provided_files: tuple[str, ...]
    excluded_files: tuple[str, ...]
    exclusion_reasons: tuple[str, ...]
    for name in ("provided_files", "excluded_files", "exclusion_reasons"):
        rows = payload.get(name)
        if not isinstance(rows, list) or any(not isinstance(row, str) for row in rows):
            raise ValueError("review artifact scope list invalid")
        values = tuple(rows)
        if name == "provided_files":
            provided_files = values
        elif name == "excluded_files":
            excluded_files = values
        else:
            exclusion_reasons = values
    diff_truncated: bool
    file_list_truncated: bool
    context_truncated: bool
    collection_incomplete: bool
    required_context_truncated: bool
    content_redacted: bool
    for name in ("diff_truncated", "file_list_truncated", "context_truncated", "collection_incomplete", "required_context_truncated", "content_redacted"):
        if type(payload.get(name)) is not bool:
            raise ValueError("review artifact scope flag invalid")
        value = payload[name]
        if name == "diff_truncated":
            diff_truncated = value
        elif name == "file_list_truncated":
            file_list_truncated = value
        elif name == "context_truncated":
            context_truncated = value
        elif name == "collection_incomplete":
            collection_incomplete = value
        elif name == "required_context_truncated":
            required_context_truncated = value
        else:
            content_redacted = value
    return ReviewScope(
        total_changed_files=total,
        provided_files=provided_files,
        excluded_files=excluded_files,
        diff_truncated=diff_truncated,
        file_list_truncated=file_list_truncated,
        context_truncated=context_truncated,
        collection_incomplete=collection_incomplete,
        required_context_truncated=required_context_truncated,
        content_redacted=content_redacted,
        exclusion_reasons=exclusion_reasons,
    )


def _finding_id(finding: Any) -> str:
    stable = f"{finding.path}\x00{finding.issue}\x00{finding.suggested_fix}\x00{finding.hunk_index}\x00{finding.new_line}\x00{finding.old_line}"
    return "review_finding:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


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
        finding_id = _finding_id(finding)
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
        "required_context_truncated": bool(getattr(scope, "required_context_truncated", False)),
        "content_redacted": bool(getattr(scope, "content_redacted", False)),
        "exclusion_reasons": list(getattr(scope, "exclusion_reasons", ())),
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
    import re

    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", value) or ".." in value:
        raise ValueError("invalid artifact component")
    return value


def load_recorded_review(state_home: Any, session_id: str, run_id: str) -> Any:
    """Read finished ledger plus its verified artifact, with one-hop reuse lineage."""
    from dataclasses import replace

    from codey.runs.ledger import RunLedgerStore
    from codey.runs.ledger_projection import load_run_projection

    ledgers = RunLedgerStore(state_home)
    projection = load_run_projection(ledgers, session_id, run_id)
    if projection is None or not projection.complete or projection.review is None:
        return None
    summary = projection.review
    artifact_run = summary.source_run_id if summary.origin == "reused" else run_id
    if not artifact_run or not summary.artifact_sha256:
        return None
    if artifact_run != run_id:
        original = load_run_projection(ledgers, session_id, artifact_run)
        if (original is None or not original.complete or original.review is None
            or original.stop_reason != "done" or original.project != projection.project
            or original.review.origin != "fresh"
            or original.review.attempt_id != summary.attempt_id
            or original.review.artifact_sha256 != summary.artifact_sha256):
            return None
    result = load_review_artifact(ReviewArtifactStore(state_home), session_id=session_id, run_id=artifact_run,
        attempt_id=summary.attempt_id, expected_sha256=summary.artifact_sha256, project=projection.project)
    if result.verdict != summary.verdict or result.status != summary.status or len(result.findings) != summary.finding_count:
        return None
    return projection, replace(result, origin=summary.origin,
                               source_run_id=artifact_run if summary.origin == "reused" else "")
