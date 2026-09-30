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


def _task_requires_modification(session: Any) -> bool:
    """An explicit goal requirement applies independently of task profile."""
    if getattr(session, "project_changes_required", False) is True:
        return True
    policy = getattr(session, "policy", None)
    return "project_changes_required" in tuple(getattr(policy, "required_checks", ()) or ())


def _verification_identity_matches(item: dict[str, Any], sess_fp: str, sess_rev: int) -> bool:
    """Exit-0 verification passes only with matching file identity.

    Both sides must carry a fingerprint; missing or mismatched identity
    never passes. Tasks that need no verification return not_applicable
    upstream instead of relying on this helper.
    """
    ver_fp = str(item.get("workspace_fingerprint", "") or "")
    ver_rev = item.get("workspace_revision")
    if not ver_fp or not sess_fp:
        return False
    try:
        from codey.workspace.revision import valid_workspace_fingerprint as _valid_fp
    except Exception:
        _valid_fp = None  # type: ignore[assignment]
    if _valid_fp is not None and (not _valid_fp(ver_fp) or not _valid_fp(sess_fp)):
        return False
    if ver_fp != sess_fp:
        return False
    try:
        if ver_rev is not None and sess_rev and int(ver_rev) != int(sess_rev):
            return False
    except (TypeError, ValueError):
        return False
    return True


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


def _verification_targets_latest_edit(session: Any, latest: Any) -> bool:
    try:
        edited = dict(getattr(session, "edited_files", {}) or {})
    except Exception:
        return True
    latest_edit = max((int(v) for v in edited.values()), default=0) if edited else 0
    try:
        ver_rev = int(latest.get("revision", -1))
    except (TypeError, ValueError):
        return False
    return not edited or ver_rev == latest_edit


def _verification_exit_code(latest: Any) -> int | None:
    exit_code = latest.get("exit_code", None)
    if exit_code is None:
        return None
    try:
        from codey.utils.refs import strict_exit_code as _strict_exit

        return _strict_exit(exit_code)
    except Exception:
        return None


def _evidence_workspace_identity(evidence: Any) -> tuple[int, str, bool]:
    rev = getattr(evidence, "workspace_revision", 0)
    fp = str(getattr(evidence, "workspace_fingerprint", "") or "")
    try:
        from codey.workspace.revision import valid_workspace_fingerprint
    except Exception:
        valid_workspace_fingerprint = None  # type: ignore[assignment]
    if not fp:
        return rev, fp, False
    if valid_workspace_fingerprint is not None and not valid_workspace_fingerprint(fp):
        return rev, fp, False
    return rev, fp, True


def _session_identity_matches(session: Any, latest: Any, rev: int, fp: str) -> bool:
    try:
        from codey.workspace.revision import valid_workspace_fingerprint
    except Exception:
        valid_workspace_fingerprint = None  # type: ignore[assignment]
    ver_fp = str(latest.get("workspace_fingerprint", "") or "")
    if ver_fp:
        if valid_workspace_fingerprint is not None:
            return bool(valid_workspace_fingerprint(ver_fp)) and ver_fp == fp
        return ver_fp == fp
    try:
        sess_fp = str(getattr(session, "workspace_fingerprint", "") or "")
        sess_rev = int(getattr(session, "workspace_revision", 0) or 0)
    except Exception:
        return False
    if sess_fp and sess_fp != fp:
        return False
    if sess_rev and int(rev or 0) and sess_rev != int(rev or 0):
        return False
    return bool(sess_fp or ver_fp)


def _append_session_check(evidence: Any, command: str, exit_code: int, rev: int, fp: str) -> Any:
    try:
        from codey.runtime.observe.execution_evidence import CheckEvidence
    except Exception:
        return evidence
    item = CheckEvidence(command, ".", exit_code=exit_code, workspace_revision=rev, workspace_fingerprint=fp)
    try:
        existing = list(getattr(evidence, "checks_after_edit", []) or [])
    except Exception:
        return evidence
    for row in existing:
        try:
            if str(getattr(row, "command", "") or "") == command:
                return evidence
        except Exception:
            continue
    import contextlib as _contextlib2

    try:
        evidence._append_check(evidence.checks_after_edit, item)
    except Exception:
        with _contextlib2.suppress(Exception):
            evidence.checks_after_edit.append(item)
    return evidence


def _evidence_with_session_facts(evidence: Any, session: Any) -> Any:
    """Project session verifications into execution evidence for the engine.

    Only real execution facts with a matching workspace identity complete.
    A missing or invalid workspace fingerprint stays not_run; the gate never
    synthesizes a format-valid fingerprint (e.g. sha256("kernel-session:…"))
    because format-valid does not mean it matches the actual file version.
    """
    latest = _session_latest_verification(session)
    if latest is None or evidence is None:
        return evidence
    if not _verification_targets_latest_edit(session, latest):
        return evidence
    exit_code = _verification_exit_code(latest)
    if exit_code is None:
        return evidence
    from codey.utils.refs import strict_verification_success

    if not strict_verification_success(
        latest.get("passed"), exit_code, passed_present="passed" in latest
    ):
        return evidence
    command = str(latest.get("command", "") or "").strip()[:500]
    if not command:
        return evidence
    rev, fp, valid = _evidence_workspace_identity(evidence)
    if not valid:
        return evidence
    if not _session_identity_matches(session, latest, rev, fp):
        return evidence
    return _append_session_check(evidence, command, exit_code, rev, fp)


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
