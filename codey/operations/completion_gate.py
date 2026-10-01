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
    CHECK_PASS,
    MAX_COMPLETION_CHECKS,
    CompletionCheck,
    CompletionProof,
    build_completion_contract,
    completion_check,
    completion_domains,
    project_completion_proof,
    register_completion_domain,
)
from codey.operations.project_completion_checks import project_completion_checks
from codey.operations.research_completion_checks import (
    finalize_research_text,
    source_requirement_checks,
    strict_research_checks,
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


def _proof_evidence_refs(session: Any, context: Any) -> tuple[str, ...]:
    """最终收据绑定的实际任务引用：run 身份 + 结果收据引用（refs only）。

    edit 引用携带工作区 revision，verification 引用携带 workspace 身份，
    唯一指向实际执行；描述标签（edit:路径）不再单独作为完成证据。
    """
    refs: list[str] = []
    try:
        get = (lambda k: context.get(k)) if isinstance(context, dict) else (lambda k: getattr(context, k, None)) if context is not None else (lambda k: None)
        run_id = str(get("run_id") or "")[:80] if context is not None else ""
        if run_id:
            refs.append(f"run:{run_id}")
    except Exception:
        pass
    try:
        edited = dict(getattr(session, "edited_files", {}) or {})
        for path, rev in sorted(
            ((str(k)[:120], v) for k, v in edited.items()),
            key=lambda row: row[0],
        )[:8]:
            try:
                rev_num = int(rev)
            except (TypeError, ValueError):
                rev_num = -1
            refs.append(f"edit:{path}@rev{rev_num}" if rev_num >= 0 else f"edit:{path}")
    except Exception:
        pass
    try:
        for url in sorted(str(u)[:160] for u in (getattr(session, "opened_sources", set()) or set()))[:8]:
            refs.append(f"source:{url}")
    except Exception:
        pass
    try:
        verifs = list(getattr(session, "verifications", ()) or [])[-4:]
        for item in verifs:
            if isinstance(item, dict):
                command = str(item.get("command", "") or "")[:80]
                exit_code = item.get("exit_code", "?")
                ws_rev = item.get("workspace_revision")
                ws_fp = str(item.get("workspace_fingerprint") or "")
                identity = ""
                if type(ws_rev) is int and ws_rev >= 0:
                    identity = f"@ws{ws_rev}"
                if not identity and ws_fp:
                    identity = f"@{ws_fp[:24]}"
                refs.append(f"verify:{command}{identity}:{exit_code}")
    except Exception:
        pass
    return tuple(refs[:12])


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
    Reading the policy's required checks never means "no requirements".
    """
    try:
        policy = getattr(session, "policy", None)
        required = tuple(getattr(policy, "required_checks", ()) or ())
    except Exception as exc:
        return GateVerdict(
            complete=False,
            followup=(
                "Not done yet (required checks unreadable: "
                f"{type(exc).__name__}). Collect the missing local evidence, "
                "then propose done again."
            ),
            proof=None,
        )
    required = tuple(str(r or "").strip() for r in required if str(r or "").strip())
    if len(required) > int(MAX_COMPLETION_CHECKS):
        return GateVerdict(
            complete=False,
            followup=(
                f"Not done yet (too many required checks: {len(required)} > {MAX_COMPLETION_CHECKS}). "
                "Reduce the task's required checks to at most "
                f"{MAX_COMPLETION_CHECKS}, then propose done again."
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


def _collect_provider_checks(session: Any, profile: str, checks: list[CompletionCheck]) -> None:
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
            else:
                illegal = completion_check(f"{key}_error", CHECK_FAIL, "check_provider_error")
                if illegal is not None:
                    checks.append(illegal)


def _dedupe_checks(checks: list[CompletionCheck]) -> list[CompletionCheck]:
    seen: set[tuple[str, str]] = set()
    deduped: list[CompletionCheck] = []
    for row in checks:
        key = (row.check_id, row.status)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _contract_subject(session: Any, profile: str, context: Any) -> str:
    try:
        get = (lambda k: context.get(k)) if isinstance(context, dict) else (lambda k: getattr(context, k, None))
        run_id = str(get("run_id") or "")[:80]
    except Exception:
        run_id = ""
    if run_id:
        return f"run:{run_id}:task:{profile or 'task'}"
    # 无 run 身份即无唯一定位：只保留 profile，不再用任务文本摘要伪造身份。
    return f"task:{profile or 'task'}"


def _evaluate_inner(session: Any, done_text: object, *, context: Any = None) -> GateVerdict:
    text = str(done_text or "").strip()
    text = finalize_research_text(session, text, context)
    checks: list[CompletionCheck] = []
    checks.extend(project_completion_checks(session, context))
    checks.extend(source_requirement_checks(session, context))
    checks.extend(strict_research_checks(session, text, context))
    profile = _session_profile(session)
    _collect_provider_checks(session, profile, checks)
    deduped = _dedupe_checks(checks)
    # Task-declared required checks gate before the contract: the task entry
    # must prove its own checks ran, not just any globally registered check.
    required_block = _required_checks_verdict(session, deduped)
    if required_block is not None:
        return required_block
    domain = _domain_for_session(session)
    subject = _contract_subject(session, profile, context)
    evidence_refs = _proof_evidence_refs(session, context)
    get = context.get if isinstance(context, dict) else lambda key: getattr(context, key, None)
    coding_evaluation = get("project_evaluation")
    coding_proof = coding_evaluation.decision.proof if coding_evaluation is not None else None
    refs = {
        key: getattr(coding_proof, key, ())
        for key in ("limitation_refs", "finding_refs", "analysis_run_refs",
                    "artifact_refs", "external_refs", "diagnostic_refs")
    }
    if coding_proof is not None:
        evidence_refs = (*coding_proof.evidence_refs, *evidence_refs)
    try:
        contract = build_completion_contract(domain=domain, subject_ref=subject, checks=deduped, evidence_refs=evidence_refs, **refs)
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
    "source_requirement_checks",
    "strict_research_checks",
    "unregister_completion_check_provider",
]
