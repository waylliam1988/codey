from __future__ import annotations

import json

from codey.completion.contract import (
    CHECK_FAIL,
    CHECK_NOT_APPLICABLE,
    CHECK_NOT_RUN,
    CHECK_PASS,
    COMPLETION_BLOCKED,
    COMPLETION_COMPLETE,
    COMPLETION_COMPLETE_WITH_LIMITATIONS,
    COMPLETION_FAILED,
    CompletionContract,
    build_completion_contract,
    completion_check,
    completion_proof_trace_payload,
    project_completion_proof,
)


def _contract(checks, **kwargs) -> CompletionContract:
    return build_completion_contract(
        domain=kwargs.pop("domain", "coding"),
        subject_ref=kwargs.pop("subject_ref", "run:abc"),
        checks=checks,
        **kwargs,
    )


def test_completion_check_fails_closed_on_invalid_input() -> None:
    assert completion_check("relevant_verification", "pass") is not None
    assert completion_check("relevant_verification", "model_says_done") is None
    assert completion_check("", "pass") is None
    assert completion_check(None, None) is None


def test_all_passed_checks_complete_without_limitations() -> None:
    contract = _contract([
        completion_check("changed_files_collected", CHECK_PASS),
        completion_check("relevant_verification", CHECK_PASS),
    ])
    proof = project_completion_proof(contract)

    assert proof is not None
    assert proof.status == COMPLETION_COMPLETE
    assert proof.satisfied is True
    assert proof.blocked_reason == ""
    assert proof.reason_codes == ()


def test_failed_check_marks_proof_failed_with_reason() -> None:
    contract = _contract([
        completion_check("changed_files_collected", CHECK_PASS),
        completion_check("relevant_verification", CHECK_FAIL, "relevant_verification_failed"),
    ])
    proof = project_completion_proof(contract)

    assert proof is not None
    assert proof.status == COMPLETION_FAILED
    assert proof.satisfied is False
    assert proof.blocked_reason == "relevant_verification_failed"
    assert proof.reason_codes == ("relevant_verification_failed",)


def test_not_run_check_blocks_with_its_reason_code() -> None:
    contract = _contract([
        completion_check("relevant_verification", CHECK_NOT_RUN, "no_matching_verification_command"),
    ])
    proof = project_completion_proof(contract)

    assert proof is not None
    assert proof.status == COMPLETION_BLOCKED
    assert proof.satisfied is False
    assert proof.blocked_reason == "no_matching_verification_command"


def test_not_applicable_checks_do_not_block() -> None:
    contract = _contract([
        completion_check("relevant_verification", CHECK_NOT_APPLICABLE),
        completion_check("deliverable_present", CHECK_PASS),
    ])
    proof = project_completion_proof(contract)

    assert proof is not None
    assert proof.status == COMPLETION_COMPLETE


def test_complete_with_limitations_requires_limitation_refs() -> None:
    checks = [completion_check("relevant_verification", CHECK_PASS)]
    limited = _contract(
        checks,
        limitation_refs=("docs_only_change",),
    )
    plain = _contract(checks)
    proof_limited = project_completion_proof(limited)
    proof_plain = project_completion_proof(plain)

    assert proof_limited.status == COMPLETION_COMPLETE_WITH_LIMITATIONS
    assert proof_limited.satisfied is False
    assert proof_limited.limitation_refs == ("docs_only_change",)
    assert proof_plain.status == COMPLETION_COMPLETE
    assert proof_plain.satisfied is True


def test_empty_checks_fail_closed_to_blocked() -> None:
    contract = build_completion_contract(
        domain="coding",
        subject_ref="run:abc",
        checks=[],
    )
    assert contract is None
    assert project_completion_proof(None) is None


def test_contract_rejects_unknown_domain_and_subject() -> None:
    assert build_completion_contract(
        domain="chatting",
        subject_ref="run:abc",
        checks=[completion_check("x", CHECK_PASS)],
    ) is None
    assert build_completion_contract(
        domain="coding",
        subject_ref="",
        checks=[completion_check("x", CHECK_PASS)],
    ) is None


def test_contract_ids_are_deterministic_and_content_addressed() -> None:
    checks_a = [completion_check("a", CHECK_PASS)]
    first = _contract(list(checks_a))
    second = _contract(list(checks_a))
    different = _contract([completion_check("a", CHECK_FAIL)])

    assert first is not None and second is not None and different is not None
    assert first.contract_id == second.contract_id
    assert first.contract_id.startswith("completion_contract:")
    assert len(first.contract_id.split(":")[1]) == 16
    assert different.contract_id != first.contract_id

    proofs = {project_completion_proof(contract).proof_id for contract in (first, second)}
    assert len(proofs) == 1


def test_contract_id_covers_every_ref_group() -> None:
    # Proofs derive their id from the contract id and RunTrace dedupes by
    # proof id: if any payload ref group were excluded from the hash, two
    # provably different proofs could collapse into one row.
    base = {
        "evidence_refs": (),
        "limitation_refs": (),
        "finding_refs": (),
        "analysis_run_refs": (),
        "artifact_refs": (),
        "external_refs": (),
    }
    reference = _contract(
        [completion_check("a", CHECK_PASS)],
        **base,
    )
    variants = [
        dict(base, evidence_refs=("ledger:r1",)),
        dict(base, limitation_refs=("docs_only_change",)),
        dict(base, finding_refs=("review_finding:" + "a" * 16,)),
        dict(base, analysis_run_refs=("analysis_run:" + "b" * 16,)),
        dict(base, artifact_refs=("artifact_version:" + "c" * 16,)),
        dict(base, external_refs=("receipt:r1",)),
    ]
    assert reference is not None
    ids = {reference.contract_id}
    for kwargs in variants:
        contract = _contract([completion_check("a", CHECK_PASS)], **kwargs)
        assert contract is not None
        assert contract.contract_id != reference.contract_id, kwargs
        ids.add(contract.contract_id)
    assert len(ids) == len(variants) + 1


def test_safe_run_ref_is_domain_neutral_and_redacts_secrets() -> None:
    from codey.completion.contract import safe_run_ref

    assert safe_run_ref("") == ""
    assert safe_run_ref("run-123") == "run-123"
    secret = safe_run_ref("token=SECRET_VALUE_123")
    assert "SECRET" not in secret
    assert len(secret) == 16
    assert all(char in "0123456789abcdef" for char in secret)


def test_duplicate_check_rows_are_deduplicated_and_capped() -> None:
    rows = [
        completion_check(f"check_{index}", CHECK_PASS)
        for index in range(20)
    ]
    contract = _contract([*rows, *rows[:2]])

    assert contract is not None
    assert len(contract.checks) == 12
    check_ids = [row.check_id for row in contract.checks]
    assert len(set(check_ids)) == len(check_ids)


def test_proof_trace_payload_is_refs_only_and_json_serializable() -> None:
    contract = _contract(
        [
            completion_check("relevant_verification", CHECK_FAIL, "relevant_verification_failed"),
        ],
        evidence_refs=("ledger:run-1",),
        limitation_refs=(),
        finding_refs=("review_finding:" + "a" * 16,),
        analysis_run_refs=("analysis_run:" + "b" * 16,),
        artifact_refs=("artifact_version:" + "c" * 16,),
        external_refs=("receipt:run-1",),
    )
    payload = completion_proof_trace_payload(project_completion_proof(contract))

    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload["status"] == COMPLETION_FAILED
    assert payload["blocked_reason"] == "relevant_verification_failed"
    assert payload["finding_refs"] == ["review_finding:" + "a" * 16]
    # No free-text fields: everything is ids, statuses, codes, and refs.
    allowed_keys = {
        "proof_id",
        "contract_id",
        "domain",
        "status",
        "satisfied",
        "blocked_reason",
        "reason_codes",
        "checks",
        "subject_ref",
        "evidence_refs",
        "limitation_refs",
        "finding_refs",
        "analysis_run_refs",
        "artifact_refs",
        "external_refs",
    }
    assert set(payload) <= allowed_keys
    for row in payload["checks"]:
        assert set(row) <= {"check_id", "status", "reason_code"}
    assert isinstance(serialized, str)


def test_trace_payload_derives_satisfied_from_status() -> None:
    failed_payload = completion_proof_trace_payload({
        "proof_id": "completion_proof:" + "a" * 16,
        "contract_id": "completion_contract:" + "b" * 16,
        "domain": "coding",
        "status": COMPLETION_FAILED,
        "satisfied": True,
        "checks": [completion_check("relevant_verification", CHECK_FAIL).to_payload()],
    })
    limited_payload = completion_proof_trace_payload({
        "proof_id": "completion_proof:" + "c" * 16,
        "contract_id": "completion_contract:" + "d" * 16,
        "domain": "coding",
        "status": COMPLETION_COMPLETE_WITH_LIMITATIONS,
        "satisfied": True,
        "checks": [completion_check("relevant_verification", CHECK_PASS).to_payload()],
        "limitation_refs": ("verification_not_locally_observed",),
    })
    complete_payload = completion_proof_trace_payload({
        "proof_id": "completion_proof:" + "e" * 16,
        "contract_id": "completion_contract:" + "f" * 16,
        "domain": "coding",
        "status": COMPLETION_COMPLETE,
        "satisfied": False,
        "checks": [completion_check("relevant_verification", CHECK_PASS).to_payload()],
    })

    assert failed_payload["satisfied"] is False
    assert limited_payload["satisfied"] is False
    assert complete_payload["satisfied"] is True


def test_proof_trace_payload_handles_junk_input() -> None:
    assert completion_proof_trace_payload(None) == {}
    assert completion_proof_trace_payload({}) == {}
    assert completion_proof_trace_payload("junk") == {}
