from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypedDict, cast

from codey.research.counter_status import counter_investigation_incomplete
from codey.research.evidence_runtime import normalize_runtime_ref as _normalize_runtime_ref
from codey.research.proof_helpers import _positive_ints
from codey.utils.refs import bounded_refs, clip, identifier, nonnegative_int

if TYPE_CHECKING:
    from codey.research.proof_quality import ProofDiagnostic

class _RelationReview(TypedDict):
    citation_present: bool
    citation_locator_verified: bool
    support_relation_verified: bool
    counterevidence_checked: bool
    supported_claim_ids: frozenset[str]
    hard_failures: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    diagnostics: tuple[ProofDiagnostic, ...]

def _proof_diagnostic(**kwargs: object) -> ProofDiagnostic:
    from codey.research.proof_quality import ProofDiagnostic
    return ProofDiagnostic(**cast(Any, kwargs))

def _append_relation_hard(hard: list[str], diagnostics: list[ProofDiagnostic], claims: Mapping[str, Mapping[str, object]], evidence: Mapping[str, Mapping[str, object]], sources: Mapping[str, Mapping[str, object]], reason_code: str, *, claim_ref: str='', evidence_ref: str='', source_ref: str='', relation_ref: str='') -> None:
    hard.append(reason_code)
    diagnostics.append(_proof_diagnostic(reason_code=reason_code, claim_ref=claim_ref if claim_ref in claims else '', evidence_ref=evidence_ref if evidence_ref in evidence else '', source_ref=source_ref if source_ref in sources else '', relation_ref=_normalize_runtime_ref(relation_ref, kind='relation')))

def _check_support_relation(from_ref: str, to_ref: str, relation_id: str, evidence: Mapping[str, Mapping[str, object]], source_ids: set[str], support_by_claim: dict[str, set[str]], hard: list[str], diagnostics: list[ProofDiagnostic], claims: Mapping[str, Mapping[str, object]], sources: Mapping[str, Mapping[str, object]]) -> None:
    ev = evidence.get(to_ref)
    if ev is None:
        _append_relation_hard(hard, diagnostics, claims, evidence, sources, 'support_relation_missing_evidence', claim_ref=from_ref, relation_ref=relation_id)
        return
    if identifier(ev.get('stance'), 40) != 'supports':
        _append_relation_hard(hard, diagnostics, claims, evidence, sources, 'support_relation_wrong_stance', claim_ref=from_ref, evidence_ref=to_ref, relation_ref=relation_id)
        return
    if not _evidence_locator_ok(ev, source_ids):
        _append_relation_hard(hard, diagnostics, claims, evidence, sources, 'support_relation_bad_locator', claim_ref=from_ref, evidence_ref=to_ref, source_ref=identifier(ev.get('source_id'), 80), relation_ref=relation_id)
        return
    support_by_claim.setdefault(from_ref, set()).add(to_ref)

def _check_counter_relation(to_ref: str, relation_id: str, evidence: Mapping[str, Mapping[str, object]], assumption_ids: set[str], source_ids: set[str], hard: list[str], diagnostics: list[ProofDiagnostic], claims: Mapping[str, Mapping[str, object]], sources: Mapping[str, Mapping[str, object]]) -> bool:
    if to_ref in evidence:
        ev = evidence[to_ref]
        if _evidence_locator_ok(ev, source_ids):
            return True
        _append_relation_hard(hard, diagnostics, claims, evidence, sources, 'counter_relation_bad_locator', evidence_ref=to_ref, source_ref=identifier(ev.get('source_id'), 80), relation_ref=relation_id)
        return False
    if to_ref in assumption_ids:
        return True
    _append_relation_hard(hard, diagnostics, claims, evidence, sources, 'counter_relation_missing_target', relation_ref=relation_id)
    return False

def _review_relation_rows(relations: tuple[Mapping[str, object], ...], claims: Mapping[str, Mapping[str, object]], evidence: Mapping[str, Mapping[str, object]], sources: Mapping[str, Mapping[str, object]], source_ids: set[str], assumption_ids: set[str], support_by_claim: dict[str, set[str]], hard: list[str], diagnostics: list[ProofDiagnostic]) -> bool:
    counter_checked = False
    for relation in relations:
        kind = identifier(relation.get('relation_kind'), 40)
        from_ref = identifier(relation.get('from_ref'), 80)
        to_ref = identifier(relation.get('to_ref'), 80)
        relation_id = identifier(relation.get('relation_id'), 80)
        if from_ref not in claims:
            _append_relation_hard(hard, diagnostics, claims, evidence, sources, 'relation_missing_claim', relation_ref=relation_id)
            continue
        if kind == 'supports':
            _check_support_relation(from_ref, to_ref, relation_id, evidence, source_ids, support_by_claim, hard, diagnostics, claims, sources)
        elif kind in {'refutes', 'limits'} and _check_counter_relation(to_ref, relation_id, evidence, assumption_ids, source_ids, hard, diagnostics, claims, sources) and not counter_investigation_incomplete(str(claims[from_ref].get('claim_text', ''))):
            counter_checked = True
    return counter_checked

def _check_claim_refs(claim_id: str, evidence_refs: set[str], assumption_refs: set[str], evidence_ids: set[str], assumption_ids: set[str], hard: list[str], diagnostics: list[ProofDiagnostic], claims: Mapping[str, Mapping[str, object]], evidence: Mapping[str, Mapping[str, object]], sources: Mapping[str, Mapping[str, object]]) -> None:
    missing_evidence_refs = evidence_refs - evidence_ids
    missing_assumption_refs = assumption_refs - assumption_ids
    if missing_evidence_refs:
        _append_relation_hard(hard, diagnostics, claims, evidence, sources, 'claim_missing_evidence_ref', claim_ref=claim_id)
    if missing_assumption_refs:
        _append_relation_hard(hard, diagnostics, claims, evidence, sources, 'claim_missing_assumption_ref', claim_ref=claim_id)

def _check_required_claim(claim_id: str, section: str, citations: tuple[int, ...], evidence_refs: set[str], status: str, support_by_claim: dict[str, set[str]], supported_claim_ids: set[str], hard: list[str], diagnostics: list[ProofDiagnostic], claims: Mapping[str, Mapping[str, object]], evidence: Mapping[str, Mapping[str, object]], sources: Mapping[str, Mapping[str, object]]) -> tuple[bool, bool]:
    if section not in {'conclusion', 'evidence'}:
        return (False, False)
    if not citations:
        _append_relation_hard(hard, diagnostics, claims, evidence, sources, 'claim_missing_citation', claim_ref=claim_id)
    if status == 'assumption':
        _append_relation_hard(hard, diagnostics, claims, evidence, sources, 'assumption_used_as_answer', claim_ref=claim_id)
    if status != 'evidence_backed':
        _append_relation_hard(hard, diagnostics, claims, evidence, sources, 'claim_not_evidence_backed', claim_ref=claim_id)
    if not evidence_refs:
        _append_relation_hard(hard, diagnostics, claims, evidence, sources, 'claim_missing_evidence_ref', claim_ref=claim_id)
    support_refs = support_by_claim.get(claim_id, set())
    claim_support_refs = support_refs & evidence_refs
    if support_refs and (not claim_support_refs):
        _append_relation_hard(hard, diagnostics, claims, evidence, sources, 'support_relation_not_claim_evidence', claim_ref=claim_id)
    if status == 'evidence_backed' and claim_support_refs:
        supported_claim_ids.add(claim_id)
        return (True, True)
    _append_relation_hard(hard, diagnostics, claims, evidence, sources, 'claim_missing_support_relation', claim_ref=claim_id)
    return (True, False)

def _review_claim_rows(claims: Mapping[str, Mapping[str, object]], evidence: Mapping[str, Mapping[str, object]], sources: Mapping[str, Mapping[str, object]], evidence_ids: set[str], assumption_ids: set[str], support_by_claim: dict[str, set[str]], supported_claim_ids: set[str], hard: list[str], diagnostics: list[ProofDiagnostic]) -> tuple[bool, int, int, bool]:
    citation_present = False
    required_claims = 0
    supported_required = 0
    counter_from_claims = False
    for claim_id, claim in claims.items():
        section = identifier(claim.get('claim_section'), 80)
        citations = _positive_ints(claim.get('citation_numbers'))
        evidence_refs = set(bounded_refs(claim.get('evidence_refs', ()), limit=48))
        assumption_refs = set(bounded_refs(claim.get('assumption_refs', ()), limit=48))
        status = identifier(claim.get('status'), 40) or 'unsupported'
        if citations:
            citation_present = True
        _check_claim_refs(claim_id, evidence_refs, assumption_refs, evidence_ids, assumption_ids, hard, diagnostics, claims, evidence, sources)
        is_required, is_supported = _check_required_claim(claim_id, section, citations, evidence_refs, status, support_by_claim, supported_claim_ids, hard, diagnostics, claims, evidence, sources)
        if is_required:
            required_claims += 1
        if is_supported:
            supported_required += 1
        if section == 'counter' and (status == 'assumption' or assumption_refs) and not counter_investigation_incomplete(str(claim.get('claim_text', ''))):
            counter_from_claims = True
    return (citation_present, required_claims, supported_required, counter_from_claims)

def _finalize_relation_verdict(required_claims: int, supported_required: int, supported_claim_ids: set[str], support_by_claim: dict[str, set[str]], claims: Mapping[str, Mapping[str, object]], evidence: Mapping[str, Mapping[str, object]], source_ids: set[str]) -> tuple[bool, bool]:
    support_relation_verified = bool(required_claims and supported_required == required_claims)
    used_evidence = {item for claim_id in supported_claim_ids for item in support_by_claim.get(claim_id, set()) if item in set(bounded_refs(claims[claim_id].get('evidence_refs', ()), limit=48))}
    locator_verified = bool(used_evidence) and all(_evidence_locator_ok(evidence[item], source_ids) for item in used_evidence if item in evidence)
    return support_relation_verified, locator_verified

def _review_relations(*, claims: Mapping[str, Mapping[str, object]], evidence: Mapping[str, Mapping[str, object]], assumptions: Mapping[str, Mapping[str, object]], relations: tuple[Mapping[str, object], ...], sources: Mapping[str, Mapping[str, object]]) -> _RelationReview:
    source_ids = set(sources)
    evidence_ids = set(evidence)
    assumption_ids = set(assumptions)
    support_by_claim: dict[str, set[str]] = {}
    supported_claim_ids: set[str] = set()
    hard: list[str] = []
    diagnostics: list[ProofDiagnostic] = []
    counter_from_relations = _review_relation_rows(relations, claims, evidence, sources, source_ids, assumption_ids, support_by_claim, hard, diagnostics)
    citation_present, required_claims, supported_required, counter_from_claims = _review_claim_rows(claims, evidence, sources, evidence_ids, assumption_ids, support_by_claim, supported_claim_ids, hard, diagnostics)
    counter_checked = counter_from_relations or counter_from_claims
    support_relation_verified, locator_verified = _finalize_relation_verdict(required_claims, supported_required, supported_claim_ids, support_by_claim, claims, evidence, source_ids)
    return {'citation_present': citation_present, 'citation_locator_verified': locator_verified, 'support_relation_verified': support_relation_verified, 'counterevidence_checked': counter_checked, 'supported_claim_ids': frozenset(supported_claim_ids), 'hard_failures': tuple(dict.fromkeys(hard)), 'missing_evidence': tuple(), 'diagnostics': tuple(diagnostics)}

def _evidence_locator_ok(evidence: Mapping[str, object], source_ids: set[str]) -> bool:
    source_id = identifier(evidence.get('source_id'), 80)
    locator = evidence.get('locator')
    if not source_id or source_id not in source_ids or (not isinstance(locator, Mapping)):
        return False
    locator_source = identifier(locator.get('source_id'), 80)
    if locator_source != source_id:
        return False
    kind = identifier(locator.get('kind'), 40)
    if not kind or kind == 'unknown':
        return False
    start = nonnegative_int(locator.get('char_start'))
    end = nonnegative_int(locator.get('char_end'))
    page = locator.get('page')
    span_ok = end > start
    page_ok = isinstance(page, int) and page > 0
    locator_text_ok = bool(clip(locator.get('locator'), 80))
    return span_ok or page_ok or locator_text_ok
