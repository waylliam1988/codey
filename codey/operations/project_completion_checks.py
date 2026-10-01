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
from codey.runtime.observe.execution_evidence import CheckEvidence
from codey.workspace.revision import valid_workspace_fingerprint, valid_workspace_revision


def _workspace_identity_equal(ver_rev: object, ver_fp: object, cur_rev: object, cur_fp: object) -> bool:
    """One strict workspace identity rule for every verification path.

    Both revisions must be exact ``int`` (``bool`` rejected) with a valid
    workspace value and equal; both fingerprints must be valid and equal.
    Missing or malformed identity never matches.
    """
    if type(ver_rev) is not int or type(cur_rev) is not int:
        return False
    if not valid_workspace_revision(ver_rev) or not valid_workspace_revision(cur_rev):
        return False
    if int(ver_rev) != int(cur_rev):
        return False
    ver_fp_text = str(ver_fp or "")
    cur_fp_text = str(cur_fp or "")
    if not valid_workspace_fingerprint(ver_fp_text) or not valid_workspace_fingerprint(cur_fp_text):
        return False
    return ver_fp_text == cur_fp_text


def _task_requires_modification(session: Any) -> bool:
    """An explicit goal requirement applies independently of task profile."""
    if getattr(session, "project_changes_required", False) is True:
        return True
    policy = getattr(session, "policy", None)
    return "project_changes_required" in tuple(getattr(policy, "required_checks", ()) or ())


def _verification_identity_matches(item: dict[str, Any], sess_fp: str, sess_rev: int) -> bool:
    """Exit-0 verification passes only with matching file identity.

    Both sides must carry a complete (revision, fingerprint) pair; missing
    or mismatched identity never passes. Tasks that need no verification
    return not_applicable upstream instead of relying on this helper.
    """
    return _workspace_identity_equal(
        item.get("workspace_revision"), item.get("workspace_fingerprint"), sess_rev, sess_fp,
    )


def _refresh_completion_workspace(session: Any, context: Any) -> None:
    """Compare verification with files observed at the completion boundary."""
    from pathlib import Path

    from codey.workspace.revision import workspace_fingerprint

    get = context.get if isinstance(context, dict) else lambda key: getattr(context, key, None)
    # Post-review evaluation already refreshed the operation's evidence.
    if get("project_evaluation") is not None or not session.edited_files:
        return
    root = getattr(session, "project", "") or get("project")
    if not root or not Path(root).is_dir():
        return
    fingerprint = workspace_fingerprint(root, ignored_paths=get("workspace_ignored_paths") or ())
    session.set_workspace_state(session.workspace_revision, fingerprint)
    evidence = get("execution_evidence")
    if evidence is not None:
        evidence.set_workspace_state(session.workspace_revision, fingerprint)


def _fresh_verification_verdict(
    session: Any,
    verifications: list[Any],
    scope: tuple[str, ...],
    latest_revision: int,
    session_fingerprint: str,
    session_revision: int,
) -> tuple[bool, bool]:
    latest_by_requirement: dict[tuple[str, str], dict[str, Any]] = {}
    for item in verifications:
        if not isinstance(item, dict):
            continue
        try:
            if int(item.get("revision", -1)) != latest_revision or item.get("exit_code") is None:
                continue
        except (TypeError, ValueError):
            continue
        key = (str(item.get("command") or "").strip(), str(item.get("cwd") or ".").strip() or ".")
        latest_by_requirement[key] = item
    fresh_pass = False
    fresh_fail = False
    from codey.utils.refs import strict_exit_code

    for item in latest_by_requirement.values():
        try:
            code = strict_exit_code(item.get("exit_code"))
        except Exception:
            code = None
        valid = (
            code is not None
            and item.get("passed") is not False
            and _session_check_covers_candidate(session, item, scope)
            and _verification_identity_matches(item, session_fingerprint, session_revision)
        )
        if not valid or code != 0:
            fresh_fail = True
        elif code == 0:
            fresh_pass = True
    return fresh_pass, fresh_fail


def _session_check_covers_candidate(session: Any, item: dict[str, Any], scope: tuple[str, ...]) -> bool:
    selected = getattr(session, "selected_verification", None)
    if selected is None:
        return True
    from codey.completion.verification_policy import check_covers_selected_candidate

    return check_covers_selected_candidate(selected, str(item.get("command") or ""),
        str(item.get("cwd") or "."), scope, root=getattr(session, "project", None) or None)


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
        try:
            rev = int(item.get("revision", -1))
        except (TypeError, ValueError):
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


def _synthesize_selected_check(provided: Any, session: Any) -> Any:
    if provided is not None:
        return provided
    latest = _session_latest_verification(session)
    if latest is None:
        return None
    command = str(latest.get("command", "") or "").strip()
    if not command:
        return None
    try:
        from codey.completion.verification_policy import VerificationCandidate
    except Exception:
        return None
    try:
        return VerificationCandidate(command=command[:240], cwd=".", source="kernel_session")
    except Exception:
        return None


def _evidence_with_session_facts(evidence: Any, session: Any) -> Any:
    """Project ordered session verifications into execution evidence.

    Every verification that targets the latest edit is replayed in recorded
    order through the single ``ExecutionEvidence.observe_check`` owner,
    preserving its original command, cwd, exit code, and workspace identity.
    Success and failure share that path so the latest observation for one
    (command, cwd) replaces the earlier one: success-then-failure blocks and
    failure-then-success passes, with or without an evidence context. Invalid
    identities never become passing evidence; freshness against the current
    workspace is decided by the engine over the projected rows.
    """
    if evidence is None:
        return evidence
    try:
        edited = dict(getattr(session, "edited_files", {}) or {})
    except Exception:
        return evidence
    if not edited:
        return evidence
    try:
        latest_edit = max(int(v) for v in edited.values())
    except (TypeError, ValueError):
        return evidence
    try:
        verifications = list(getattr(session, "verifications", ()) or [])
    except Exception:
        return evidence
    if not verifications:
        return evidence
    from codey.utils.refs import strict_verification_success

    observe = getattr(evidence, "observe_check", None)
    if not callable(observe):
        return evidence
    for row in verifications:
        if not isinstance(row, dict):
            continue
        try:
            if int(row.get("revision", -1)) != latest_edit:
                continue
        except (TypeError, ValueError):
            continue
        command = str(row.get("command", "") or "").strip()[:500]
        cwd = str(row.get("cwd", ".") or ".").strip()[:240] or "."
        exit_code = row.get("exit_code")
        if type(exit_code) is not int:
            continue
        ver_rev = row.get("workspace_revision")
        ver_fp = str(row.get("workspace_fingerprint", "") or "")
        if type(ver_rev) is not int or not valid_workspace_revision(ver_rev):
            continue
        if not valid_workspace_fingerprint(ver_fp):
            continue
        if not command:
            continue
        succeeded = strict_verification_success(
            row.get("passed"), exit_code, passed_present="passed" in row,
        )
        item = CheckEvidence(
            command, cwd, exit_code=exit_code,
            workspace_revision=ver_rev, workspace_fingerprint=ver_fp,
        )
        observe(item, succeeded=succeeded)
    return evidence


def _engine_checks(session: Any, context: Any) -> list[CompletionCheck] | None:
    get = (lambda key: context.get(key)) if isinstance(context, dict) else (lambda key: getattr(context, key, None))
    provided = get("project_evaluation")
    if provided is not None:
        proof = provided.decision.proof
        return list(proof.checks) if proof is not None else None
    evidence = get("execution_evidence")
    if evidence is None:
        return None
    try:
        from codey.completion.engine import CompletionEngine
    except Exception as exc:
        row = completion_check("completion_engine", CHECK_NOT_RUN, f"engine_unavailable:{type(exc).__name__}")
        return [row] if row is not None else []
    try:
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
        changes = _synthesize_changes(scope, get("changes"))
        selected = _synthesize_selected_check(get("selected_check"), session)
        # Single engine call over the complete projection: session facts fill
        # the gaps the unified production context does not carry, so the same
        # edit+fresh-pass passes with or without an evidence-only context.
        # Old-version verification (stale revision) stays unobserved because
        # _session_latest_verification only reflects the latest revision and
        # the engine still requires freshness against the current scope.
        effective_evidence = _evidence_with_session_facts(evidence, session)
        # When there is no edited scope at all, fall back to the session
        # checks (not_applicable) instead of forcing an engine_empty.
        if not scope and not task_changed:
            return None
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
            verification_forbidden=bool(get("verification_forbidden")),
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
    _refresh_completion_workspace(session, context)
    if context is not None:
        real = _engine_checks(session, context)
        if real is not None:
            return real
    edited = dict(getattr(session, "edited_files", {}) or {})
    verifs = list(getattr(session, "verifications", ()) or [])
    if not edited:
        if _task_requires_modification(session):
            row = completion_check("project_changes_required", CHECK_FAIL, "project_changes_required")
            return [row] if row is not None else []
        row = completion_check("relevant_verification", CHECK_NOT_APPLICABLE)
        return [row] if row is not None else []
    if getattr(session, "verification_forbidden", False) is True:
        row = completion_check("relevant_verification", CHECK_NOT_APPLICABLE, "verification_forbidden_by_request")
        return [row] if row is not None else []
    try:
        latest = max(int(v) for v in edited.values())
    except (TypeError, ValueError):
        row = completion_check("relevant_verification", CHECK_NOT_RUN, "verification_not_fresh")
        return [row] if row is not None else []
    try:
        sess_fp = str(getattr(session, "workspace_fingerprint", "") or "")
        sess_rev = int(getattr(session, "workspace_revision", 0) or 0)
    except Exception:
        sess_fp, sess_rev = "", 0
    fresh_pass, fresh_fail = _fresh_verification_verdict(
        session, verifs, tuple(edited), latest, sess_fp, sess_rev,
    )
    if fresh_pass and not fresh_fail:
        row = completion_check("relevant_verification", CHECK_PASS)
    elif fresh_fail:
        row = completion_check("relevant_verification", CHECK_FAIL, "relevant_verification_failed")
    else:
        row = completion_check("relevant_verification", CHECK_NOT_RUN, "verification_not_fresh")
    return [row] if row is not None else []

__all__ = ["project_completion_checks"]
