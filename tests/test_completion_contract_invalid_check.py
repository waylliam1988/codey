from codey.completion.contract import (
    CHECK_PASS,
    CompletionCheck,
    build_completion_contract,
    project_completion_proof,
)


def test_invalid_check_status_cannot_make_a_satisfied_contract() -> None:
    passed = CompletionCheck("tests", CHECK_PASS)
    malformed = CompletionCheck("format", "unknown")
    valid = build_completion_contract(domain="coding", subject_ref="run-1", checks=(passed,))
    proof = project_completion_proof(valid)
    assert proof is not None and proof.satisfied

    mixed = build_completion_contract(domain="coding", subject_ref="run-1", checks=(passed, malformed))
    assert mixed is None
    assert project_completion_proof(mixed) is None
