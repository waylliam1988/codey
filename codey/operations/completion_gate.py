"""Single completion entry: one done, one proof from combined checks.

The model's ``done`` only proposes completion; local facts decide. Task kinds
declare required checks via pluggable providers; the gate builds one
``CompletionContract`` and projects one ``CompletionProof``. Exceeding the
check cap fails closed (no silent drops). Model text never proves tests pass
or sources opened, except that the final report text itself carries sections.
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

_PROVIDERS: dict[str, CheckProvider] = {}


def register_completion_check_provider(name: object, fn: CheckProvider) -> bool:
    key = str(name or "").strip()
    if not key or not callable(fn):
        return False
    _PROVIDERS[key] = fn
    return True


def make_check(check_id: object, status: object, reason: object = "") -> CompletionCheck | None:
    return completion_check(check_id, status, reason)


def _coding_checks(session: Any) -> list[CompletionCheck]:
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
        if bool(item.get("passed")):
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


def _research_checks(session: Any, done_text: str) -> list[CompletionCheck]:
    policy = getattr(session, "policy", None)
    strict = bool(getattr(policy, "strict_research", False))
    if not strict:
        return []
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


def _domain_for_session(session: Any) -> str:
    kind = str(getattr(session, "task_kind", "") or "").strip().lower()
    if kind == "research":
        return "research"
    if kind in {"planning", "planning_readonly", "readonly"}:
        register_completion_domain("planning")
        return "planning"
    if kind in completion_domains():
        return kind
    # Custom kinds register their own domain via register_completion_domain;
    # fall back to coding without editing the kernel.
    return "coding"


@dataclass(frozen=True)
class GateVerdict:
    complete: bool
    followup: str
    proof: CompletionProof | None


def evaluate(session: Any, done_text: object) -> GateVerdict:
    text = str(done_text or "").strip()
    checks: list[CompletionCheck] = []
    checks.extend(_coding_checks(session))
    checks.extend(_research_checks(session, text))
    for _name, provider in list(_PROVIDERS.items()):
        try:
            produced = provider(session)
        except Exception:
            continue
        if not isinstance(produced, (list, tuple)):
            continue
        for row in produced:
            if isinstance(row, CompletionCheck):
                checks.append(row)
    # Deduplicate by (check_id, status), keeping first occurrence.
    seen: set[tuple[str, str]] = set()
    deduped: list[CompletionCheck] = []
    for row in checks:
        key = (row.check_id, row.status)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    domain = _domain_for_session(session)
    subject = f"task:{str(getattr(session, 'task_kind', '') or 'task').strip().lower() or 'task'}"
    contract = build_completion_contract(domain=domain, subject_ref=subject, checks=deduped)
    if contract is None:
        if len(deduped) == 0:
            followup = "No completion checks were produced; cannot complete yet. Continue the task."
        else:
            followup = (
                "Completion contract invalid (too many checks or unknown domain); "
                "cannot complete yet. Continue with the most important evidence or verification."
            )
        return GateVerdict(complete=False, followup=followup, proof=None)
    proof = project_completion_proof(contract)
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
]
