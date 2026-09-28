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


def _coding_checks(session: Any, context: Any = None) -> list[CompletionCheck]:
    if context is not None:
        real = _engine_checks(session, context)
        if real is not None:
            return real
    edited = dict(getattr(session, "edited_files", {}) or {})
    verifs = list(getattr(session, "verifications", ()) or [])
    if not edited:
        row = completion_check("relevant_verification", CHECK_NOT_APPLICABLE)
        return [row] if row is not None else []
    try:
        latest = max(int(v) for v in edited.values())
    except (TypeError, ValueError):
        row = completion_check("relevant_verification", CHECK_NOT_RUN, "verification_not_fresh")
        return [row] if row is not None else []
    fresh_pass = False
    fresh_fail = False
    for item in verifs:
        if not isinstance(item, dict):
            continue
        try:
            rev = int(item.get("revision", -1))
        except (TypeError, ValueError):
            continue
        if rev != latest:
            continue
        if item.get("exit_code", None) is not None:
            try:
                if int(item["exit_code"]) == 0:
                    fresh_pass = True
                else:
                    fresh_fail = True
            except (TypeError, ValueError):
                fresh_fail = True
        elif bool(item.get("passed")):
            fresh_pass = True
        else:
            fresh_fail = True
    if fresh_pass and not fresh_fail:
        row = completion_check("relevant_verification", CHECK_PASS)
    elif fresh_fail:
        row = completion_check("relevant_verification", CHECK_FAIL, "relevant_verification_failed")
    else:
        row = completion_check("relevant_verification", CHECK_NOT_RUN, "verification_not_fresh")
    return [row] if row is not None else []


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
        engine = CompletionEngine()
        result = engine.evaluate(
            run_id=str(get("run_id") or ""),
            task=str(get("task") or ""),
            changes=get("changes"),
            stop_reason="done",
            task_changed=bool(get("task_changed")),
            scope_files=tuple(get("scope_files") or ()),
            selected_check=get("selected_check"),
            evidence=evidence,
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
    if not strict:
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
        from codey.research.proof_quality import review_research_proof

        record = get("research_record")
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


def evaluate(session: Any, done_text: object, *, context: Any = None) -> GateVerdict:
    try:
        return _evaluate_inner(session, done_text, context=context)
    except Exception as exc:
        return GateVerdict(
            complete=False,
            followup=f"Completion check failed ({type(exc).__name__}); cannot complete yet. Continue the task.",
            proof=None,
        )


def _evaluate_inner(session: Any, done_text: object, *, context: Any = None) -> GateVerdict:
    text = str(done_text or "").strip()
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
        return GateVerdict(complete=True, followup="", proof=proof)
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
