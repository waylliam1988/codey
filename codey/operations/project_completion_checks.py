"""Project completion checks for the unified gate (operations layer).

Owns project/edit/verification and engine evidence checks. Never mints
a completion proof and never imports the gate: the gate combines these
rows into the final contract.
"""

from __future__ import annotations

from collections.abc import Sequence

from codey.completion.contract import (
    CHECK_FAIL,
    CHECK_NOT_APPLICABLE,
    CHECK_NOT_RUN,
    CHECK_PASS,
    CompletionCheck,
    completion_check,
)
from codey.completion.engine import CompletionEngine
from codey.completion.inputs import ChangedFile, CompletionChangeSet
from codey.completion.verification_policy import VerificationCandidate
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_inputs import ProjectCompletionFacts
from codey.operations.task_session import TaskSession
from codey.runtime.core.verification_observation import (
    CompleteVerification,
    IncompleteVerification,
    VerificationObservation,
    decode_verification_observation,
    latest_verification_observations,
    verification_observations,
)
from codey.runtime.observe.execution_evidence import CheckEvidence, ExecutionEvidence


def _task_requires_modification(session: TaskSession | ProjectCompletionFacts) -> bool:
    """An explicit goal requirement applies independently of task profile."""
    if session.project_changes_required:
        return True
    return "project_changes_required" in session.policy.required_checks


def collect_completion_workspace(session: TaskSession, context: CompletionContext) -> None:
    """Compare verification with files observed at the completion boundary."""
    from pathlib import Path

    from codey.workspace.revision import workspace_fingerprint

    if not session.edited_files:
        return
    root = session.project or context.project
    if not root or not Path(root).is_dir():
        return
    fingerprint = workspace_fingerprint(root, ignored_paths=context.workspace_ignored_paths or ())
    session.set_workspace_state(session.workspace_revision, fingerprint)
    evidence = context.execution_evidence
    if evidence is not None:
        evidence.set_workspace_state(session.workspace_revision, fingerprint)


def _effective_verification_forbidden(session: TaskSession | ProjectCompletionFacts) -> bool:
    """One owner for the verification exemption: the session task requirement."""
    return session.verification_forbidden


def _session_scope_files(session: TaskSession | ProjectCompletionFacts) -> tuple[str, ...]:
    return tuple(path for path in session.edited_files if path)


def _session_latest_verification(session: TaskSession | ProjectCompletionFacts) -> VerificationObservation | None:
    rows = verification_observations(session.verifications)
    latest: VerificationObservation | None = None
    latest_revision = -1
    for row in rows:
        if row.edit_revision is not None and row.edit_revision >= latest_revision:
            latest, latest_revision = row, row.edit_revision
    return latest


def _synthesize_changes(
    scope: tuple[str, ...], provided: CompletionChangeSet | None,
) -> CompletionChangeSet | None:
    if provided is not None and provided.files:
        return provided
    if not scope:
        return provided
    return CompletionChangeSet(tuple(ChangedFile(path) for path in scope),
                               changed_count=len(scope), mode="kernel")


def _selected_equal(first: VerificationCandidate, second: VerificationCandidate) -> bool:
    return (first.command.strip() == second.command.strip()
            and (first.cwd or ".").strip() == (second.cwd or ".").strip())


def _resolve_selected_check(session: TaskSession | ProjectCompletionFacts, context: CompletionContext) -> VerificationCandidate | None:
    """One owner for the required verification candidate.

    The session requirement wins; a context candidate only fills a missing
    requirement. Two differing requirements block explicitly instead of
    letting the latest run redefine what the task requires. With neither
    requirement, the latest observation forms the default candidate with
    its real command and cwd preserved.
    """
    provided = context.selected_check
    selected = session.selected_verification
    if selected is not None and provided is not None and not _selected_equal(selected, provided):
        raise ValueError("selected verification conflict: session and context disagree")
    if selected is not None:
        return selected
    if provided is not None:
        return provided
    latest = _session_latest_verification(session)
    if latest is None:
        return None
    command = latest.command
    if not command:
        return None
    cwd = latest.cwd
    return VerificationCandidate(command=command, cwd=cwd, source="kernel_session")


def _evidence_with_session_facts(
    evidence: ExecutionEvidence | None, session: TaskSession | ProjectCompletionFacts,
) -> tuple[ExecutionEvidence | None, tuple[str, ...]]:
    """Project every ordered valid observation, excluding incomplete latest keys.

    An incomplete observation is retained by the decoder. It blocks the entire
    key, so a previous success cannot revive after a newer unknown result.
    Operation-confirmed recovered scope can use the observations' edit epoch
    when the in-memory edited-file index has not yet been reconstructed.
    """
    if evidence is None:
        return None, ()
    observations = verification_observations(session.verifications)
    if not observations:
        return evidence, ()
    exact_edits = [value for value in session.edited_files.values() if type(value) is int]
    revisions = exact_edits or [row.edit_revision for row in observations if row.edit_revision is not None]
    if not revisions:
        return evidence, ()
    latest_edit = max(revisions)
    latest = latest_verification_observations(observations, latest_edit)
    gap_keys, gaps = _projection_gaps_for_latest(latest)
    for row in observations:
        if row.edit_revision != latest_edit or row.key in gap_keys:
            continue
        outcome = row.outcome
        if not isinstance(outcome, CompleteVerification):
            continue
        evidence.observe_check(CheckEvidence(
            row.command, row.cwd, exit_code=outcome.exit_code,
            workspace_revision=outcome.workspace_revision,
            workspace_fingerprint=outcome.workspace_fingerprint,
        ), succeeded=outcome.succeeded)
    return evidence, tuple(gaps)


def _latest_observations_by_key(
    verifications: Sequence[object], latest_edit: int,
) -> dict[tuple[str, str], VerificationObservation]:
    """Legacy record boundary; the projection itself only accepts domain rows."""
    return latest_verification_observations(verification_observations(verifications), latest_edit)


def _gap_for_latest_row(row: object, command: str) -> str:
    """Compatibility codec used by language-neutral legacy wire fixtures."""
    decoded = decode_verification_observation(row)
    if not command or decoded is None:
        return "verification_result_missing"
    return decoded.outcome.gap if isinstance(decoded.outcome, IncompleteVerification) else ""


def verification_gap_diagnostics(session: TaskSession | ProjectCompletionFacts) -> str:
    """Bounded explanation from the same decoded observations as the gate."""
    latest_edit = max([0, *session.edited_files.values()])
    latest = _latest_observations_by_key(session.verifications, latest_edit)
    details = [f"{row.outcome.gap}: command={row.command[:160]!r}, cwd={row.cwd[:80]!r}"
               for row in latest.values() if isinstance(row.outcome, IncompleteVerification)]
    return "\n".join(details[:4])


def _projection_gaps_for_latest(
    latest_by_key: dict[tuple[str, str], VerificationObservation],
) -> tuple[set[tuple[str, str]], list[str]]:
    gap_keys: set[tuple[str, str]] = set()
    gaps: list[str] = []
    for key, row in latest_by_key.items():
        if not isinstance(row.outcome, IncompleteVerification):
            continue
        gap_keys.add(key)
        if row.outcome.gap not in gaps:
            gaps.append(row.outcome.gap)
    return gap_keys, gaps


def _normalized_scope_and_change(session: TaskSession | ProjectCompletionFacts, context: CompletionContext) -> tuple[tuple[str, ...], bool]:
    scope = tuple(dict.fromkeys((*tuple(context.scope_files or ()), *_session_scope_files(session))))
    provided_changed = context.task_changed
    if provided_changed is None:
        task_changed = bool(scope)
    else:
        try:
            task_changed = bool(provided_changed) or bool(scope)
        except Exception:
            task_changed = bool(scope)
    return scope, task_changed


def _engine_checks(session: TaskSession | ProjectCompletionFacts, context: CompletionContext) -> list[CompletionCheck]:
    try:
        scope, task_changed = _normalized_scope_and_change(session, context)
        rows: list[CompletionCheck] = []
        if _task_requires_modification(session):
            change_check = completion_check(
                "project_changes_required", CHECK_PASS if task_changed else CHECK_FAIL,
                "" if task_changed else "project_changes_required",
            )
            if change_check is not None:
                rows.append(change_check)
            if not task_changed:
                return rows
        return rows + _verification_checks(session, context, scope, task_changed)
    except Exception as exc:
        row = completion_check("completion_engine", CHECK_FAIL, f"engine_error:{type(exc).__name__}")
        return [row] if row is not None else []


def _verification_checks(
    session: TaskSession | ProjectCompletionFacts, context: CompletionContext, scope: tuple[str, ...], task_changed: bool,
) -> list[CompletionCheck]:
    # A failed candidate refresh leaves the requirement untrustworthy:
    # block explicitly instead of falling back to the latest run.
    if session.verification_candidates_refresh_failed is True:
        row = completion_check(
            "relevant_verification", CHECK_FAIL, "verification_candidates_refresh_failed",
        )
        return [row] if row is not None else []
    if not scope and not task_changed:
        row = completion_check("relevant_verification", CHECK_NOT_APPLICABLE)
        return [row] if row is not None else []
    evidence = context.execution_evidence
    if evidence is None:
        evidence = ExecutionEvidence(
            workspace_revision=session.workspace_revision,
            workspace_fingerprint=session.workspace_fingerprint,
        )
    else:
        evidence = evidence.detached()
    changes = _synthesize_changes(scope, context.changes)
    selected = _resolve_selected_check(session, context)
    # Single engine call over the complete projection: session facts fill
    # the gaps the unified production context does not carry, so the same
    # edit+fresh-pass passes with or without an evidence-only context.
    # An incomplete latest observation is an explicit projection gap:
    # block without consulting older success for the same check.
    effective_evidence, projection_gaps = _evidence_with_session_facts(evidence, session)
    if effective_evidence is None:
        raise ValueError("verification evaluation has no execution evidence")
    if projection_gaps:
        reason = (projection_gaps[0] or "verification_identity_missing")
        row = completion_check("relevant_verification", CHECK_NOT_RUN, reason)
        return [row] if row is not None else []
    engine = CompletionEngine()
    result = engine.evaluate(
        run_id=str(context.run_id or ""),
        task=str(context.task or ""),
        changes=changes.to_payload() if changes is not None else None,
        stop_reason="done",
        task_changed=task_changed,
        scope_files=scope,
        selected_check=selected,
        evidence=effective_evidence,
        analysis_run_payloads=tuple(row.to_payload() for row in context.analysis_run_payloads),
        project=context.project,
        checkpoint_green=bool(context.checkpoint_green),
        verification_forbidden=_effective_verification_forbidden(session),
    )
    proof = result.decision.proof
    rows: list[CompletionCheck] = list(proof.checks) if proof is not None else []
    if result.integrity.diagnostic_refs:
        fallback = completion_check("edit_integrity", CHECK_PASS)
    else:
        fallback = completion_check("relevant_verification", CHECK_NOT_RUN, "engine_empty")
    return rows or ([fallback] if fallback is not None else [])


def project_completion_checks(session: TaskSession, context: object = None) -> list[CompletionCheck]:
    """Single verification decision for every caller.

    Evidence presence only completes inputs; the rule is identical with or
    without an evidence context. There is no second session-only verdict.
    """
    context = CompletionContext.from_value(context)
    collect_completion_workspace(session, context)
    rows = project_evidence_checks(session, context)
    from codey.operations.explicit_execution_requirements import requested_output_read_check

    requested_read = requested_output_read_check(session, str(context.task or session.task_text))
    if requested_read is not None:
        rows.append(requested_read)
    return rows


def project_evidence_checks(session: TaskSession, context: CompletionContext) -> list[CompletionCheck]:
    rows = evaluate_project_completion(ProjectCompletionFacts.capture(session), context)
    from pathlib import Path

    from codey.operations.browser_copy import copy_target
    from codey.toolchain.runtime import WRITE_MAX_FILE_BYTES, safe_join
    from codey.workspace.paths import content_hash, read_text_bounded

    target = copy_target(session.task_text)
    if (target is None or session.selected_verification is not None or context.selected_check is not None
            or set(session.edited_files) - {target.path} or set(context.scope_files) - {target.path}):
        return rows
    digest = next((value for result in reversed(tuple(session._memory_results.values()))
                   if result.ok and result.call.name == "edit" and result.call.args.get("path") == target.path
                   and "browser_source" in result.call.args
                   and isinstance(value := result.audit.get("browser_copy_content_hash"), str)), "")
    if not digest or not (root := session.project or context.project):
        return rows
    try:
        current = read_text_bounded(safe_join(Path(root), target.path), max_bytes=WRITE_MAX_FILE_BYTES)
        matches = content_hash(current) == digest
    except (OSError, ValueError):
        matches = False
    return [*(row for row in rows if row.check_id != "relevant_verification"),
            CompletionCheck("relevant_verification", CHECK_PASS if matches else CHECK_FAIL,
                            "browser_copy_content_verified" if matches else "browser_copy_content_changed")]


def evaluate_project_completion(session: ProjectCompletionFacts, context: CompletionContext) -> list[CompletionCheck]:
    """Evaluate collected facts without scanning files or mutating evidence."""
    return _engine_checks(session, context)

__all__ = ["project_completion_checks"]
