"""Single completion entry: one done, one proof from combined checks.

The model's ``done`` only proposes completion; local facts decide. Checks are
scoped per task profile (no global provider leaks across kinds). Coding reuses
the production completion engine over real execution evidence when a kernel
context is supplied; strict Research reuses the evidence ledger, the report
finalizer, and the research contract. Any import failure, check exception, or
over-cap proof fails closed to blocked, never complete.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from codey.completion.contract import (
    CHECK_FAIL,
    CHECK_NOT_APPLICABLE,
    CHECK_NOT_RUN,
    CHECK_PASS,
    CompletionCheck,
    CompletionProof,
    build_completion_contract,
    completion_check,
    completion_domains,
    project_completion_proof,
    register_completion_domain,
)

CheckProvider = Callable[[Any], list[CompletionCheck]]

_PROVIDERS: dict[str, tuple[CheckProvider, frozenset[str] | None]] = {}


def register_completion_check_provider(
    name: object,
    fn: CheckProvider,
    *,
    profile: object = None,
) -> bool:
    key = str(name or "").strip()
    if not key or not callable(fn):
        return False
    profiles: frozenset[str] | None = None
    if profile is not None:
        if isinstance(profile, str):
            wanted = {profile.strip().lower()} if profile.strip() else set()
        else:
            try:
                wanted = {str(item or "").strip().lower() for item in profile}
            except TypeError:
                wanted = set()
            wanted.discard("")
        profiles = frozenset(wanted)
    _PROVIDERS[key] = (fn, profiles)
    return True


def unregister_completion_check_provider(name: object) -> bool:
    key = str(name or "").strip()
    if not key or key not in _PROVIDERS:
        return False
    del _PROVIDERS[key]
    return True


def make_check(check_id: object, status: object, reason: object = "") -> CompletionCheck | None:
    return completion_check(check_id, status, reason)


def _session_profile(session: Any) -> str:
    return str(getattr(session, "task_kind", "") or "").strip().lower() or "project"


def _task_requires_modification(session: Any) -> bool:
    """Explicit entry requirement; never keyword-guessed.

    The task entry sets ``project_changes_required`` (and/or a
    ``project_changes_required`` required_check). Write permission alone
    never implies modification, so read-only tasks like
    "检查 bug，不要修改任何文件" stay completable without edits.
    """
    try:
        if bool(getattr(session, "project_changes_required", False) is True):
            try:
                kind = str(getattr(session, "task_kind", "") or "").strip().lower()
            except Exception:
                kind = ""
            return kind in {"project", "hybrid"}
    except Exception:
        pass
    try:
        policy = getattr(session, "policy", None)
        required = tuple(getattr(policy, "required_checks", ()) or ())
        if "project_changes_required" in {str(r or "").strip() for r in required}:
            try:
                kind = str(getattr(session, "task_kind", "") or "").strip().lower()
            except Exception:
                kind = ""
            return kind in {"project", "hybrid"}
    except Exception:
        pass
    return False


def _verification_identity_matches(item: dict[str, Any], sess_fp: str, sess_rev: int) -> bool:
    """Exit-0 verification passes only with matching file identity.

    When neither side carries a fingerprint (workspace-less unit sessions),
    exit 0 passes for backward compatibility; mixed/mismatched stays not_run.
    """
    ver_fp = str(item.get("workspace_fingerprint", "") or "")
    ver_rev = item.get("workspace_revision")
    if ver_fp or sess_fp:
        if not (ver_fp and sess_fp):
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


def _coding_checks(session: Any, context: Any = None) -> list[CompletionCheck]:
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


def _research_checks(session: Any, done_text: str, context: Any = None) -> list[CompletionCheck]:
    policy = getattr(session, "policy", None)
    strict = bool(getattr(policy, "strict_research", False))
    source_required = bool(getattr(policy, "sources_open_required", False))
    if not strict and not source_required:
        return []
    if context is not None:
        real = _ledger_checks(session, done_text, context)
        if real is not None:
            return real
    opened = set(getattr(session, "opened_sources", set()) or set())
    evidence = list(getattr(session, "evidence", []) or [])
    rows: list[CompletionCheck] = []
    row = completion_check(
        "research_sources_opened",
        CHECK_PASS if opened else CHECK_NOT_RUN,
        "" if opened else "research_source_not_opened",
    )
    if row is not None:
        rows.append(row)
    row = completion_check(
        "research_evidence_saved",
        CHECK_PASS if evidence else CHECK_NOT_RUN,
        "" if evidence else "research_evidence_missing",
    )
    if row is not None:
        rows.append(row)
    text = str(done_text or "")
    has_sections = "结论" in text and "来源" in text
    row = completion_check(
        "research_report_sections",
        CHECK_PASS if has_sections else CHECK_FAIL,
        "" if has_sections else "research_report_incomplete",
    )
    if row is not None:
        rows.append(row)
    return rows


def _ledger_checks(session: Any, done_text: str, context: Any) -> list[CompletionCheck] | None:
    get = (lambda key: context.get(key)) if isinstance(context, dict) else (lambda key: getattr(context, key, None))
    ledger = get("research_ledger")
    if ledger is None:
        return None
    rows: list[CompletionCheck] = []
    try:
        finals = set(ledger.final_url_set())
    except Exception as exc:
        row = completion_check("research_ledger", CHECK_FAIL, f"ledger_error:{type(exc).__name__}")
        return [row] if row is not None else []
    try:
        citable = _ledger_citable_urls(ledger, finals)
    except Exception as exc:
        row = completion_check("research_evidence", CHECK_FAIL, f"evidence_error:{type(exc).__name__}")
        return [row] if row is not None else []
    row = completion_check(
        "research_sources_opened",
        CHECK_PASS if finals else CHECK_NOT_RUN,
        "" if finals else "research_source_not_opened",
    )
    if row is not None:
        rows.append(row)
    row = completion_check(
        "research_evidence_saved",
        CHECK_PASS if citable else CHECK_NOT_RUN,
        "" if citable else "research_evidence_missing",
    )
    if row is not None:
        rows.append(row)
    try:
        from codey.research.done_finalizer import finalize_done_answer
    except Exception as exc:
        row = completion_check("research_report", CHECK_FAIL, f"finalizer_unavailable:{type(exc).__name__}")
        rows.append(row) if row is not None else None
        return rows
    try:
        finalized = finalize_done_answer(
            str(done_text or ""), ledger,
            source_ids=dict(get("source_ids") or {}),
            question=str(get("question") or ""),
        )
    except Exception as exc:
        row = completion_check("research_report", CHECK_FAIL, f"finalizer_error:{type(exc).__name__}")
        if row is not None:
            rows.append(row)
        return rows
    if getattr(finalized, "reason", "") in {"no_report_sections", "no_citable_sources",
                                            "unmapped_numeric_refs", "unmapped_source_id_refs",
                                            "no_referenced_citable_sources"}:
        row = completion_check("research_report_sections", CHECK_FAIL, f"report_{finalized.reason}")
    elif "结论" in str(done_text or "") and "来源" in str(done_text or ""):
        row = completion_check("research_report_sections", CHECK_PASS)
    else:
        row = completion_check("research_report_sections", CHECK_FAIL, "research_report_incomplete")
    if row is not None:
        rows.append(row)
    try:
        from codey.research.contract import research_completion_checks
        from codey.research.object_model import build_research_record
        from codey.research.proof_quality import review_research_proof
        from codey.research.report_quality import review_report_quality

        record = get("research_record")
        if record is None:
            report_review = review_report_quality(
                str(done_text or ""),
                ledger=ledger,
                opened_sources=finals,
                search_result_urls=set(getattr(session, "search_results", {}).values()),
            )
            quality_row = completion_check(
                "research_report_quality",
                CHECK_PASS if report_review.ok else CHECK_FAIL,
                "" if report_review.ok else "research_report_quality_failed",
            )
            if quality_row is not None:
                rows.append(quality_row)
            record = build_research_record(
                question=str(get("question") or ""), summary=str(done_text or ""),
                ledger=ledger, review=report_review,
                run_id=str(get("run_id") or ""), project=get("project"),
                stop_reason="done",
            )
        if get("require_proof_review"):
            review = review_research_proof(record, question=str(get("question") or ""),
                                           evidence_ledger=get("evidence_ledger_payload"))
            for item in research_completion_checks(review) or ():
                if isinstance(item, CompletionCheck):
                    rows.append(item)
    except Exception as exc:
        row = completion_check("research_proof", CHECK_FAIL, f"proof_error:{type(exc).__name__}")
        if row is not None:
            rows.append(row)
    return rows


def _ledger_citable_urls(ledger: Any, finals: set[str]) -> list[str]:
    urls: list[str] = []
    for item in getattr(ledger, "evidence_items", ()) or ():
        url = str(getattr(item, "source_url", "") or "").strip()
        excerpt = str(getattr(item, "excerpt", "") or "").strip()
        if not url or not excerpt:
            continue
        try:
            from codey.research.urls import opened_url

            canonical = opened_url(ledger, url)
        except Exception:
            canonical = url
        if canonical in finals and canonical not in urls:
            urls.append(canonical)
    return urls


def _domain_for_session(session: Any) -> str:
    kind = _session_profile(session)
    if kind == "research":
        return "research"
    if kind in {"planning", "planning_readonly", "readonly"}:
        register_completion_domain("planning")
        return "planning"
    if kind in completion_domains():
        return kind
    return "coding"


@dataclass(frozen=True)
class GateVerdict:
    complete: bool
    followup: str
    proof: CompletionProof | None
    final_text: str = ""


def evaluate(session: Any, done_text: object, *, context: Any = None) -> GateVerdict:
    try:
        return _evaluate_inner(session, done_text, context=context)
    except Exception as exc:
        return GateVerdict(
            complete=False,
            followup=f"Completion check failed ({type(exc).__name__}); cannot complete yet. Continue the task.",
            proof=None,
        )


def _required_checks_verdict(session: Any, deduped: list[CompletionCheck]) -> GateVerdict | None:
    """Enforce the task's own required_checks: missing/unrun/conflict blocks.

    Each required id must be produced exactly once with pass. Missing provider,
    not_run/fail/not_applicable, duplicate conflicting statuses, and over-limit
    all fail closed with an explicit message (never silent truncation).
    """
    try:
        from codey.completion.contract import MAX_COMPLETION_CHECKS as _MAX_CHECKS
    except Exception:
        _MAX_CHECKS = 12
    try:
        policy = getattr(session, "policy", None)
        required = tuple(getattr(policy, "required_checks", ()) or ())
    except Exception:
        return None
    required = tuple(str(r or "").strip() for r in required if str(r or "").strip())
    if len(required) > int(_MAX_CHECKS):
        return GateVerdict(
            complete=False,
            followup=(
                f"Not done yet (too many required checks: {len(required)} > {_MAX_CHECKS}). "
                "Reduce the task's required checks to at most "
                f"{_MAX_CHECKS}, then propose done again."
            ),
            proof=None,
        )
    if not required:
        return None
    by_id: dict[str, list[CompletionCheck]] = {}
    for row in deduped:
        try:
            by_id.setdefault(str(row.check_id or ""), []).append(row)
        except Exception:
            continue
    for req in required:
        rows = by_id.get(req, [])
        if not rows:
            return GateVerdict(
                complete=False,
                followup=(
                    f"Not done yet (required check '{req}' was not produced). "
                    "Collect the missing local evidence or fresh verification, then propose done again. "
                    "Model claims alone do not complete."
                ),
                proof=None,
            )
        statuses = {str(r.status or "") for r in rows}
        if len(statuses) > 1:
            return GateVerdict(
                complete=False,
                followup=(
                    f"Not done yet (required check '{req}' has conflicting results). "
                    "Collect the missing local evidence or fresh verification, then propose done again."
                ),
                proof=None,
            )
        status = next(iter(statuses))
        if status != CHECK_PASS:
            reason = ""
            try:
                reason = str(rows[0].reason_code or "")
            except Exception:
                reason = ""
            return GateVerdict(
                complete=False,
                followup=(
                    f"Not done yet (required check '{req}' is {status or 'missing'}"
                    f"{f': {reason}' if reason else ''}). "
                    "Collect the missing local evidence or fresh verification, then propose done again."
                ),
                proof=None,
            )
    return None


def _finalize_research_text(session: Any, text: str, context: Any) -> str:
    """Check and publish the same evidence-compiled answer, as the old runner did."""
    if not getattr(getattr(session, "policy", None), "strict_research", False):
        return text
    get = (lambda key: context.get(key)) if isinstance(context, dict) else (lambda key: getattr(context, key, None))
    ledger = get("research_ledger")
    if ledger is None:
        return text
    from codey.research.done_finalizer import finalize_done_answer

    finalized = finalize_done_answer(text, ledger, source_ids=dict(get("source_ids") or {}),
                                    question=str(get("question") or ""), enforce_claim_support=True)
    return finalized.text.strip()


def _evaluate_inner(session: Any, done_text: object, *, context: Any = None) -> GateVerdict:
    text = str(done_text or "").strip()
    text = _finalize_research_text(session, text, context)
    checks: list[CompletionCheck] = []
    checks.extend(_coding_checks(session, context))
    checks.extend(_research_checks(session, text, context))
    profile = _session_profile(session)
    for key, (provider, profiles) in list(_PROVIDERS.items()):
        if profiles is not None and profile not in profiles:
            continue
        try:
            produced = provider(session)
        except Exception as exc:
            row = completion_check(f"{key}_error", CHECK_FAIL, f"check_provider_error:{type(exc).__name__}")
            if row is not None:
                checks.append(row)
            continue
        if not isinstance(produced, (list, tuple)):
            row = completion_check(f"{key}_error", CHECK_FAIL, "check_provider_error")
            if row is not None:
                checks.append(row)
            continue
        for row in produced:
            if isinstance(row, CompletionCheck):
                checks.append(row)
    seen: set[tuple[str, str]] = set()
    deduped: list[CompletionCheck] = []
    for row in checks:
        key = (row.check_id, row.status)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    # Task-declared required checks gate before the contract: the task entry
    # must prove its own checks ran, not just any globally registered check.
    required_block = _required_checks_verdict(session, deduped)
    if required_block is not None:
        return required_block
    domain = _domain_for_session(session)
    subject = f"task:{profile or 'task'}"
    try:
        contract = build_completion_contract(domain=domain, subject_ref=subject, checks=deduped)
    except Exception as exc:
        return GateVerdict(
            complete=False,
            followup=f"Completion contract failed ({type(exc).__name__}); cannot complete yet. Continue the task.",
            proof=None,
        )
    if contract is None:
        if len(deduped) == 0:
            followup = "No completion checks were produced; cannot complete yet. Continue the task."
        else:
            followup = (
                "Completion contract invalid (too many checks or unknown domain); "
                "cannot complete yet. Continue with the most important evidence or verification."
            )
        return GateVerdict(complete=False, followup=followup, proof=None)
    try:
        proof = project_completion_proof(contract)
    except Exception as exc:
        return GateVerdict(
            complete=False,
            followup=f"Completion proof failed ({type(exc).__name__}); cannot complete yet. Continue the task.",
            proof=None,
        )
    if proof is not None and proof.satisfied:
        return GateVerdict(complete=True, followup="", proof=proof, final_text=text)
    reason = ""
    if proof is not None and proof.blocked_reason:
        reason = proof.blocked_reason
    elif proof is not None and proof.reason_codes:
        reason = proof.reason_codes[0]
    followup = (
        f"Not done yet ({reason or 'checks incomplete'}). "
        "Collect the missing local evidence or fresh verification, then propose done again. "
        "Model claims alone do not complete."
    )
    return GateVerdict(complete=False, followup=followup, proof=proof)


__all__ = [
    "GateVerdict",
    "evaluate",
    "make_check",
    "register_completion_check_provider",
    "unregister_completion_check_provider",
]
