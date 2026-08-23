"""Shared completion-contract primitives for verified completion.

This module is the domain-neutral core of the Verified Completion Gate: a
local pipeline contract records what must hold before a run may count as
complete, and a proof projects those facts into one bounded, refs-only
read model. It is a projection layer like Evidence Runtime or ContextEpoch:

- it never performs I/O, never calls models, and never executes commands;
- it never stores raw prompt text, source bodies, transcripts, or command
  output -- only statuses, reason codes, and bounded refs;
- a model claiming "done" or "tests pass" can never satisfy a check by
  itself; checks are fed from local facts by the calling layer.

Status derivation is a hard gate, not a score:

- any failed check            -> ``failed`` (reason from the first failure)
- any required-but-unrun check -> ``blocked`` (reason from the first gap)
- otherwise, with limitations  -> ``complete_with_limitations``
- otherwise                    -> ``complete``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from codey.research.identity import bounded_refs, identifier, stable_ref


COMPLETION_CONTRACT_PREFIX = "completion_contract"
COMPLETION_PROOF_PREFIX = "completion_proof"

DOMAIN_CODING = "coding"
DOMAIN_RESEARCH = "research"
DOMAIN_EXPERIMENT = "experiment"
COMPLETION_DOMAINS = frozenset({DOMAIN_CODING, DOMAIN_RESEARCH, DOMAIN_EXPERIMENT})

COMPLETION_PENDING = "pending"
COMPLETION_RUNNING = "running"
COMPLETION_BLOCKED = "blocked"
COMPLETION_COMPLETE = "complete"
COMPLETION_COMPLETE_WITH_LIMITATIONS = "complete_with_limitations"
COMPLETION_FAILED = "failed"
COMPLETION_STATUSES = frozenset(
    {
        COMPLETION_PENDING,
        COMPLETION_RUNNING,
        COMPLETION_BLOCKED,
        COMPLETION_COMPLETE,
        COMPLETION_COMPLETE_WITH_LIMITATIONS,
        COMPLETION_FAILED,
    }
)
COMPLETION_SATISFIED_STATUSES = frozenset(
    {
        COMPLETION_COMPLETE,
        COMPLETION_COMPLETE_WITH_LIMITATIONS,
    }
)

CHECK_PASS = "pass"
CHECK_FAIL = "fail"
CHECK_NOT_RUN = "not_run"
CHECK_NOT_APPLICABLE = "not_applicable"
CHECK_STATUSES = frozenset({CHECK_PASS, CHECK_FAIL, CHECK_NOT_RUN, CHECK_NOT_APPLICABLE})

MAX_COMPLETION_CHECKS = 12
MAX_COMPLETION_REASONS = 8
MAX_COMPLETION_REFS = 12
NO_COMPLETION_CHECKS_REASON = "no_completion_checks"


@dataclass(frozen=True)
class CompletionCheck:
    """One requirement's observed status. ``check_id`` names the requirement."""

    check_id: str
    status: str
    reason_code: str = ""

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "check_id": identifier(self.check_id, 80),
            "status": identifier(self.status, 20),
        }
        if self.reason_code:
            payload["reason_code"] = identifier(self.reason_code, 120)
        return payload


@dataclass(frozen=True)
class CompletionContract:
    """A local contract: what must hold before this subject counts as done."""

    contract_id: str
    domain: str
    subject_ref: str
    checks: tuple[CompletionCheck, ...]
    evidence_refs: tuple[str, ...] = ()
    limitation_refs: tuple[str, ...] = ()
    finding_refs: tuple[str, ...] = ()
    analysis_run_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    external_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompletionProof:
    """The projected outcome of one contract evaluation.

    Failed, blocked, and limited runs also produce proofs; ``status`` says
    whether the proof is satisfied. Nothing here is model-visible.
    """

    proof_id: str
    contract_id: str
    domain: str
    subject_ref: str
    status: str
    satisfied: bool
    blocked_reason: str
    reason_codes: tuple[str, ...]
    checks: tuple[CompletionCheck, ...]
    evidence_refs: tuple[str, ...]
    limitation_refs: tuple[str, ...]
    finding_refs: tuple[str, ...]
    analysis_run_refs: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    external_refs: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return completion_proof_payload(self)


def completion_check(
    check_id: object,
    status: object,
    reason_code: object = "",
) -> CompletionCheck | None:
    """Build a validated check row; invalid input fails closed to None."""

    cid = identifier(check_id, 80)
    state = identifier(status, 20)
    if not cid or state not in CHECK_STATUSES:
        return None
    return CompletionCheck(
        check_id=cid,
        status=state,
        reason_code=identifier(reason_code, 120),
    )


def build_completion_contract(
    *,
    domain: object,
    subject_ref: object,
    checks: Iterable[CompletionCheck | None],
    evidence_refs: Iterable[object] = (),
    limitation_refs: Iterable[object] = (),
    finding_refs: Iterable[object] = (),
    analysis_run_refs: Iterable[object] = (),
    artifact_refs: Iterable[object] = (),
    external_refs: Iterable[object] = (),
) -> CompletionContract | None:
    """Build a validated contract; unusable input yields None (fail closed)."""

    dom = identifier(domain, 20)
    subject = identifier(subject_ref, 160)
    rows: list[CompletionCheck] = []
    seen: set[tuple[str, str]] = set()
    for row in checks:
        item = row if isinstance(row, CompletionCheck) else None
        if item is None or (item.check_id, item.status) in seen:
            continue
        seen.add((item.check_id, item.status))
        rows.append(item)
        if len(rows) >= MAX_COMPLETION_CHECKS:
            break
    if not dom or dom not in COMPLETION_DOMAINS or not subject or not rows:
        return None
    evidence = bounded_refs(evidence_refs, limit=MAX_COMPLETION_REFS)
    limitations = bounded_refs(limitation_refs, limit=MAX_COMPLETION_REFS)
    findings = bounded_refs(finding_refs, limit=MAX_COMPLETION_REFS)
    analysis = bounded_refs(analysis_run_refs, limit=MAX_COMPLETION_REFS)
    artifacts = bounded_refs(artifact_refs, limit=MAX_COMPLETION_REFS)
    external = bounded_refs(external_refs, limit=MAX_COMPLETION_REFS)
    return CompletionContract(
        contract_id=stable_ref(
            COMPLETION_CONTRACT_PREFIX,
            dom,
            subject,
            tuple(row.to_payload() for row in rows),
            evidence,
            limitations,
        ),
        domain=dom,
        subject_ref=subject,
        checks=tuple(rows),
        evidence_refs=evidence,
        limitation_refs=limitations,
        finding_refs=findings,
        analysis_run_refs=analysis,
        artifact_refs=artifacts,
        external_refs=external,
    )


def project_completion_proof(contract: CompletionContract | None) -> CompletionProof | None:
    """Derive the deterministic hard-gate outcome of one contract."""

    if contract is None:
        return None
    failed_rows = [row for row in contract.checks if row.status == CHECK_FAIL]
    unrun_rows = [row for row in contract.checks if row.status == CHECK_NOT_RUN]
    reasons: list[str] = []
    status: str
    blocked_reason = ""
    if failed_rows:
        status = COMPLETION_FAILED
        blocked_reason = failed_rows[0].reason_code or "completion_check_failed"
        reasons.extend(row.reason_code or f"{row.check_id}_failed" for row in failed_rows)
    elif unrun_rows:
        status = COMPLETION_BLOCKED
        blocked_reason = unrun_rows[0].reason_code or "completion_check_not_run"
        reasons.extend(row.reason_code or f"{row.check_id}_not_run" for row in unrun_rows)
    elif contract.limitation_refs:
        status = COMPLETION_COMPLETE_WITH_LIMITATIONS
    else:
        status = COMPLETION_COMPLETE
    deduped = tuple(dict.fromkeys(identifier(reason, 120) for reason in reasons if reason))
    return CompletionProof(
        proof_id=stable_ref(
            COMPLETION_PROOF_PREFIX,
            contract.contract_id,
            status,
            blocked_reason,
            deduped,
        ),
        contract_id=contract.contract_id,
        domain=contract.domain,
        subject_ref=contract.subject_ref,
        status=status,
        satisfied=status in COMPLETION_SATISFIED_STATUSES,
        blocked_reason=blocked_reason,
        reason_codes=deduped,
        checks=contract.checks,
        evidence_refs=contract.evidence_refs,
        limitation_refs=contract.limitation_refs,
        finding_refs=contract.finding_refs,
        analysis_run_refs=contract.analysis_run_refs,
        artifact_refs=contract.artifact_refs,
        external_refs=contract.external_refs,
    )


def completion_proof_payload(proof: CompletionProof | None) -> dict[str, object]:
    """Bounded refs-only payload of a proof; empty groups stay omitted."""

    if proof is None:
        return {}
    payload: dict[str, object] = {
        "proof_id": proof.proof_id,
        "contract_id": proof.contract_id,
        "domain": identifier(proof.domain, 20),
        "status": identifier(proof.status, 40),
        "satisfied": bool(proof.satisfied),
        "checks": [row.to_payload() for row in proof.checks[:MAX_COMPLETION_CHECKS]],
    }
    if proof.subject_ref:
        payload["subject_ref"] = identifier(proof.subject_ref, 160)
    # A satisfied proof never carries a blocked_reason: coherence of the two
    # fields is guaranteed here, not left to callers.
    if proof.blocked_reason and not proof.satisfied:
        payload["blocked_reason"] = identifier(proof.blocked_reason, 120)
    if proof.reason_codes:
        payload["reason_codes"] = list(bounded_refs(proof.reason_codes, limit=MAX_COMPLETION_REASONS))
    for key, refs in (
        ("evidence_refs", proof.evidence_refs),
        ("limitation_refs", proof.limitation_refs),
        ("finding_refs", proof.finding_refs),
        ("analysis_run_refs", proof.analysis_run_refs),
        ("artifact_refs", proof.artifact_refs),
        ("external_refs", proof.external_refs),
    ):
        bounded = bounded_refs(refs, limit=MAX_COMPLETION_REFS)
        if bounded:
            payload[key] = list(bounded)
    return payload


def completion_proof_trace_payload(proof: object) -> dict[str, object]:
    """Trace projection accepting a CompletionProof, its payload, or a mapping.

    Payloads without resolvable proof/contract ids fail closed to an empty
    projection, so junk input never becomes a half-valid row.
    """

    if proof is None:
        return {}
    raw = proof.to_payload() if callable(getattr(proof, "to_payload", None)) else proof
    if not isinstance(raw, dict):
        return {}
    payload = completion_proof_payload(_proof_from_payload(raw))
    if not payload.get("proof_id") or not payload.get("contract_id"):
        return {}
    return payload


def _proof_from_payload(payload: dict[str, object]) -> CompletionProof | None:
    try:
        checks = tuple(
            row
            for row in (
                completion_check(
                    (item or {}).get("check_id") if isinstance(item, dict) else "",
                    (item or {}).get("status") if isinstance(item, dict) else "",
                    (item or {}).get("reason_code", "") if isinstance(item, dict) else "",
                )
                for item in payload.get("checks", ())
            )
            if row is not None
        )
        return CompletionProof(
            proof_id=str(payload.get("proof_id") or ""),
            contract_id=str(payload.get("contract_id") or ""),
            domain=str(payload.get("domain") or ""),
            subject_ref=str(payload.get("subject_ref") or ""),
            status=str(payload.get("status") or ""),
            satisfied=bool(payload.get("satisfied")),
            blocked_reason=str(payload.get("blocked_reason") or ""),
            reason_codes=tuple(str(item) for item in payload.get("reason_codes", ()) or ()),
            checks=checks,
            evidence_refs=tuple(str(item) for item in payload.get("evidence_refs", ()) or ()),
            limitation_refs=tuple(str(item) for item in payload.get("limitation_refs", ()) or ()),
            finding_refs=tuple(str(item) for item in payload.get("finding_refs", ()) or ()),
            analysis_run_refs=tuple(str(item) for item in payload.get("analysis_run_refs", ()) or ()),
            artifact_refs=tuple(str(item) for item in payload.get("artifact_refs", ()) or ()),
            external_refs=tuple(str(item) for item in payload.get("external_refs", ()) or ()),
        )
    except (TypeError, ValueError):
        return None


__all__ = [
    "CHECK_FAIL",
    "CHECK_NOT_APPLICABLE",
    "CHECK_NOT_RUN",
    "CHECK_PASS",
    "CHECK_STATUSES",
    "COMPLETION_BLOCKED",
    "COMPLETION_COMPLETE",
    "COMPLETION_COMPLETE_WITH_LIMITATIONS",
    "COMPLETION_DOMAINS",
    "COMPLETION_FAILED",
    "COMPLETION_PENDING",
    "COMPLETION_PROOF_PREFIX",
    "COMPLETION_CONTRACT_PREFIX",
    "COMPLETION_RUNNING",
    "COMPLETION_SATISFIED_STATUSES",
    "COMPLETION_STATUSES",
    "DOMAIN_CODING",
    "DOMAIN_EXPERIMENT",
    "DOMAIN_RESEARCH",
    "MAX_COMPLETION_CHECKS",
    "MAX_COMPLETION_REASONS",
    "MAX_COMPLETION_REFS",
    "NO_COMPLETION_CHECKS_REASON",
    "CompletionCheck",
    "CompletionContract",
    "CompletionProof",
    "build_completion_contract",
    "completion_check",
    "completion_proof_payload",
    "completion_proof_trace_payload",
    "project_completion_proof",
]
