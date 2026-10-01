"""Project completion checks for the unified gate (operations layer).

Owns project/edit/verification and engine evidence checks. Never mints
a completion proof and never imports the gate: the gate combines these
rows into the final contract.
"""

from __future__ import annotations

from typing import Any

from codey.completion.contract import (
    CHECK_FAIL,
    CHECK_NOT_APPLICABLE,
    CHECK_NOT_RUN,
    CHECK_PASS,
    CompletionCheck,
    completion_check,
)
from codey.completion.engine import CompletionEngine
from codey.completion.verification_policy import VerificationCandidate
from codey.runtime.observe.execution_evidence import CheckEvidence, ExecutionEvidence
from codey.workspace.revision import valid_workspace_fingerprint, valid_workspace_revision


def _task_requires_modification(session: Any) -> bool:
    """An explicit goal requirement applies independently of task profile."""
    if getattr(session, "project_changes_required", False) is True:
        return True
    policy = getattr(session, "policy", None)
    return "project_changes_required" in tuple(getattr(policy, "required_checks", ()) or ())


def _refresh_completion_workspace(session: Any, context: Any) -> None:
    """Compare verification with files observed at the completion boundary."""
    from pathlib import Path

    from codey.workspace.revision import workspace_fingerprint

    get = context.get if isinstance(context, dict) else lambda key: getattr(context, key, None)
    if not session.edited_files:
        return
    root = getattr(session, "project", "") or get("project")
    if not root or not Path(root).is_dir():
        return
    fingerprint = workspace_fingerprint(root, ignored_paths=get("workspace_ignored_paths") or ())
    session.set_workspace_state(session.workspace_revision, fingerprint)
    evidence = get("execution_evidence")
    if evidence is not None:
        evidence.set_workspace_state(session.workspace_revision, fingerprint)


def _effective_verification_forbidden(session: Any) -> bool:
    """One owner for the verification exemption: the session task requirement."""
    return getattr(session, "verification_forbidden", False) is True


def _session_scope_files(session: Any) -> tuple[str, ...]:
    try:
        edited = dict(getattr(session, "edited_files", {}) or {})
    except Exception:
        return ()
    return tuple(str(k) for k in edited if str(k))


def _session_latest_verification(session: Any) -> dict[str, Any] | None:
    try:
        verifs = list(getattr(session, "verifications", ()) or [])
    except Exception:
        return None
    if not verifs:
        return None
    latest = None
    latest_rev = -1
    for item in verifs:
        if not isinstance(item, dict):
            continue
        rev = item.get("revision", -1)
        if type(rev) is not int:
            continue
        if rev >= latest_rev:
            latest_rev = rev
            latest = item
    return latest


def _synthesize_changes(scope: tuple[str, ...], provided: Any) -> Any:
    if isinstance(provided, dict) and isinstance(provided.get("files"), list) and provided.get("files"):
        return provided
    if not scope:
        return provided
    return {
        "ok": True,
        "changed_count": len(scope),
        "files": [{"path": p} for p in scope],
        "diff": "",
        "mode": "kernel",
    }


def _selected_equal(first: Any, second: Any) -> bool:
    try:
        return (
            str(getattr(first, "command", "") or "").strip() == str(getattr(second, "command", "") or "").strip()
            and str(getattr(first, "cwd", ".") or ".").strip() == str(getattr(second, "cwd", ".") or ".").strip()
        )
    except Exception:
        return False


def _resolve_selected_check(session: Any, context: Any) -> Any:
    """One owner for the required verification candidate.

    The session requirement wins; a context candidate only fills a missing
    requirement. Two differing requirements block explicitly instead of
    letting the latest run redefine what the task requires. With neither
    requirement, the latest observation forms the default candidate with
    its real command and cwd preserved.
    """
    get = (lambda key: context.get(key)) if isinstance(context, dict) else (lambda key: getattr(context, key, None))
    provided = get("selected_check")
    selected = getattr(session, "selected_verification", None)
    if selected is not None and provided is not None and not _selected_equal(selected, provided):
        raise ValueError("selected verification conflict: session and context disagree")
    if selected is not None:
        return selected
    if provided is not None:
        return provided
    latest = _session_latest_verification(session)
    if latest is None:
        return None
    command = str(latest.get("command", "") or "").strip()
    if not command:
        return None
    cwd = str(latest.get("cwd", ".") or ".").strip() or "."
    return VerificationCandidate(command=command[:240], cwd=cwd[:240], source="kernel_session")


def _evidence_with_session_facts(evidence: Any, session: Any) -> tuple[Any, tuple[str, ...]]:
    """Project ordered session verifications into execution evidence.

    Every verification that targets the latest edit is replayed in recorded
    order through the single ``ExecutionEvidence.observe_check`` owner,
    preserving its original command, cwd, exit code, and workspace identity.
    Success and failure share that path so the latest observation for one
    (command, cwd) replaces the earlier one: success-then-failure blocks and
    failure-then-success passes, with or without an evidence context.

    The latest observation per (command, cwd) is determined first; when that
    latest row lacks workspace identity (or command/exit), its whole key is
    excluded so an old success can never revive, and a projection gap is
    returned (``verification_identity_missing`` /
    ``verification_identity_invalid`` / ``verification_result_missing``).
    Earlier incomplete rows for a key whose latest is complete stay skipped
    without blocking. Freshness against the current workspace is decided by
    the engine over the projected rows.
    """
    if evidence is None:
        return evidence, ()
    try:
        edited = dict(getattr(session, "edited_files", {}) or {})
    except Exception:
        return evidence, ()
    try:
        verifications = list(getattr(session, "verifications", ()) or [])
    except Exception:
        return evidence, ()
    if not verifications:
        return evidence, ()
    exact_edits = [v for v in edited.values() if type(v) is int]
    if exact_edits:
        latest_edit = max(exact_edits)
    else:
        # Operation-confirmed scope may exist while memory edited_files is
        # incomplete (e.g. resumed recovery). Fall back to the verifications'
        # own latest revision so a legal recovery still projects; scope and
        # task_changed already confirmed edits before reaching here.
        revs = [r.get("revision", -1) for r in verifications if isinstance(r, dict)]
        revs = [r for r in revs if type(r) is int]
        if not revs:
            return evidence, ()
        latest_edit = max(revs)
    from codey.utils.refs import strict_verification_success

    observe = getattr(evidence, "observe_check", None)
    if not callable(observe):
        return evidence, ()
    latest_by_key = _latest_observations_by_key(verifications, latest_edit)
    gap_keys, gaps = _projection_gaps_for_latest(latest_by_key)
    for row in verifications:
        if not isinstance(row, dict):
            continue
        if type(row.get("revision", -1)) is not int or row.get("revision", -1) != latest_edit:
            continue
        command = str(row.get("command", "") or "").strip()[:500]
        cwd = str(row.get("cwd", ".") or ".").strip()[:240] or "."
        if (command, cwd) in gap_keys:
            continue
        if not _is_projectable_row(row, command):
            continue
        exit_code = row.get("exit_code")
        ver_rev = row.get("workspace_revision")
        ver_fp = str(row.get("workspace_fingerprint", "") or "")
        succeeded = strict_verification_success(
            row.get("passed"), exit_code, passed_present="passed" in row,
        )
        item = CheckEvidence(
            command, cwd, exit_code=exit_code,
            workspace_revision=ver_rev, workspace_fingerprint=ver_fp,
        )
        observe(item, succeeded=succeeded)
    return evidence, tuple(gaps)


def _latest_observations_by_key(
    verifications: list[Any], latest_edit: int,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Latest row per (command, cwd) among rows targeting the latest edit."""
    latest_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in verifications:
        if not isinstance(row, dict):
            continue
        if type(row.get("revision", -1)) is not int or row.get("revision", -1) != latest_edit:
            continue
        command = str(row.get("command", "") or "").strip()[:500]
        cwd = str(row.get("cwd", ".") or ".").strip()[:240] or "."
        latest_by_key[(command, cwd)] = row
    return latest_by_key


def _gap_for_latest_row(row: dict[str, Any], command: str) -> str:
    """Gap code for one latest observation, or empty when complete."""
    exit_code = row.get("exit_code")
    if not command or type(exit_code) is not int:
        return "verification_result_missing"
    has_rev = "workspace_revision" in row and row.get("workspace_revision") is not None
    raw_fp = row.get("workspace_fingerprint")
    has_fp = "workspace_fingerprint" in row and raw_fp is not None and str(raw_fp or "") != ""
    if not has_rev or not has_fp:
        return "verification_identity_missing"
    ver_rev = row.get("workspace_revision")
    ver_fp = str(row.get("workspace_fingerprint", "") or "")
    if type(ver_rev) is not int or not valid_workspace_revision(ver_rev):
        return "verification_identity_invalid"
    if not valid_workspace_fingerprint(ver_fp):
        return "verification_identity_invalid"
    return ""


def _projection_gaps_for_latest(
    latest_by_key: dict[tuple[str, str], dict[str, Any]],
) -> tuple[set[tuple[str, str]], list[str]]:
    """Keys whose latest observation is incomplete plus deduped gap codes."""
    gap_keys: set[tuple[str, str]] = set()
    gaps: list[str] = []
    for key, row in latest_by_key.items():
        command, _cwd = key
        code = _gap_for_latest_row(row, command)
        if not code:
            continue
        gap_keys.add(key)
        if code not in gaps:
            gaps.append(code)
    return gap_keys, gaps


def _is_projectable_row(row: dict[str, Any], command: str) -> bool:
    """One individually valid row for a key whose latest is complete."""
    return _gap_for_latest_row(row, command) == ""


def _normalized_scope_and_change(session: Any, context: Any) -> tuple[tuple[str, ...], bool]:
    get = (lambda key: context.get(key)) if isinstance(context, dict) else (lambda key: getattr(context, key, None))
    scope = tuple(get("scope_files") or ())
    if not scope:
        scope = _session_scope_files(session)
    provided_changed = get("task_changed")
    if provided_changed is None:
        task_changed = bool(scope)
    else:
        try:
            task_changed = bool(provided_changed) or bool(scope)
        except Exception:
            task_changed = bool(scope)
    return scope, task_changed


def _engine_checks(session: Any, context: Any) -> list[CompletionCheck]:
    get = (lambda key: context.get(key)) if isinstance(context, dict) else (lambda key: getattr(context, key, None))
    # A precomputed project_evaluation is never a bypass: mandatory
    # requirements are always recomputed from current session facts and the
    # current context. Reuse (evidence_refs merging) happens in the gate
    # after the fresh checks pass, never as an early return here.
    try:
        scope, task_changed = _normalized_scope_and_change(session, context)
        # A failed candidate refresh leaves the requirement untrustworthy:
        # block explicitly instead of falling back to the latest run.
        if getattr(session, "verification_candidates_refresh_failed", False) is True:
            row = completion_check(
                "relevant_verification", CHECK_FAIL, "verification_candidates_refresh_failed",
            )
            return [row] if row is not None else []
        # Modification is its own requirement: forbidding verification never
        # excuses a missing edit, and review-trusted change facts arrive via
        # scope_files/task_changed so a resumed green is not mis-rejected.
        if _task_requires_modification(session) and not task_changed:
            row = completion_check("project_changes_required", CHECK_FAIL, "project_changes_required")
            return [row] if row is not None else []
        if not scope and not task_changed:
            row = completion_check("relevant_verification", CHECK_NOT_APPLICABLE)
            return [row] if row is not None else []
        evidence = get("execution_evidence")
        if evidence is None:
            evidence = ExecutionEvidence(
                workspace_revision=getattr(session, "workspace_revision", 0) or 0,
                workspace_fingerprint=str(getattr(session, "workspace_fingerprint", "") or ""),
            )
        changes = _synthesize_changes(scope, get("changes"))
        selected = _resolve_selected_check(session, context)
        # Single engine call over the complete projection: session facts fill
        # the gaps the unified production context does not carry, so the same
        # edit+fresh-pass passes with or without an evidence-only context.
        # An incomplete latest observation is an explicit projection gap:
        # block without consulting older success for the same check.
        effective_evidence, projection_gaps = _evidence_with_session_facts(evidence, session)
        if projection_gaps:
            reason = str(projection_gaps[0] or "verification_identity_missing")
            row = completion_check("relevant_verification", CHECK_NOT_RUN, reason)
            return [row] if row is not None else []
        engine = CompletionEngine()
        result = engine.evaluate(
            run_id=str(get("run_id") or ""),
            task=str(get("task") or ""),
            changes=changes,
            stop_reason="done",
            task_changed=task_changed,
            scope_files=scope,
            selected_check=selected,
            evidence=effective_evidence,
            analysis_run_payloads=get("analysis_run_payloads") or (),
            project=get("project"),
            checkpoint_green=bool(get("checkpoint_green")),
            verification_forbidden=_effective_verification_forbidden(session),
        )
        proof = result.decision.proof
        rows = [row for row in (getattr(proof, "checks", ()) or ()) if isinstance(row, CompletionCheck)]
        if result.integrity is not None and getattr(result.integrity, "diagnostic_refs", ()):
            return rows or [completion_check("edit_integrity", CHECK_PASS)]
        return rows or [completion_check("relevant_verification", CHECK_NOT_RUN, "engine_empty")]
    except Exception as exc:
        row = completion_check("completion_engine", CHECK_FAIL, f"engine_error:{type(exc).__name__}")
        return [row] if row is not None else []


def project_completion_checks(session: Any, context: Any = None) -> list[CompletionCheck]:
    """Single verification decision for every caller.

    Evidence presence only completes inputs; the rule is identical with or
    without an evidence context. There is no second session-only verdict.
    """
    _refresh_completion_workspace(session, context)
    return _engine_checks(session, context)

__all__ = ["project_completion_checks"]
