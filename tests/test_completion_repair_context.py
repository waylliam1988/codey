"""Pure completion-repair-context projection tests (0.4.13)."""

from __future__ import annotations

import json
import unittest

from codey.completion.repair_context import (
    CONTEXT_SOURCE_KEY,
    DEFAULT_REPAIR_CONTEXT_BUDGET_CHARS,
    DETAIL_MINIMAL,
    PROMPT_SOURCE_REF,
    REFUSED_NO_SAFE_CHECK_FACTS,
    REFUSED_NOT_FAILED,
    REFUSED_NOT_PRODUCT,
    DecisiveCheckFact,
    project_repair_context,
    repair_candidate,
)


def _proof_payload(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "failed",
        "proof_id": "completion_proof:" + "a" * 16,
        "contract_id": "completion_contract:" + "b" * 16,
        "reason_codes": ["relevant_verification_failed"],
        "checks": [
            {
                "check_id": "relevant_verification",
                "status": "fail",
                "reason_code": "relevant_verification_failed",
            }
        ],
    }
    payload.update(overrides)
    return payload


_FACT = DecisiveCheckFact(
    command="pytest -q",
    cwd=".",
    exit_code=1,
    result_summary="FAILED tests/test_x.py - assert 1 == 2\n1 failed in 0.4s",
)


class AdmissionTests(unittest.TestCase):
    def test_admits_only_failed_product_proofs(self) -> None:
        projection = project_repair_context(
            proof=_proof_payload(),
            failure_class="product_failure",
            decisive_checks=(_FACT,),
            changed_files=["src/foo.py"],
        )
        self.assertTrue(projection.admitted)
        self.assertIn("Completion repair context. Facts only.", projection.prompt_text)
        self.assertIn("pytest -q", projection.prompt_text)
        self.assertIn("FAILED tests/test_x.py", projection.prompt_text)
        self.assertIn("Do not treat unobserved checks as failed.", projection.prompt_text)

    def test_blocked_proof_is_refused_not_admitted(self) -> None:
        # No verification is not a bug report: an unobserved/blocked proof
        # must never become model-visible failure facts.
        projection = project_repair_context(
            proof=_proof_payload(status="blocked"),
            failure_class="product_failure",
            decisive_checks=(_FACT,),
        )
        self.assertFalse(projection.admitted)
        self.assertEqual(projection.refused_reason, REFUSED_NOT_FAILED)
        self.assertIn(REFUSED_NOT_FAILED, projection.warnings)
        self.assertEqual(projection.to_payload()["admitted"], False)

    def test_non_product_failure_class_is_refused(self) -> None:
        projection = project_repair_context(
            proof=_proof_payload(),
            failure_class="environment_failure",
            decisive_checks=(_FACT,),
        )
        self.assertFalse(projection.admitted)
        self.assertEqual(projection.refused_reason, REFUSED_NOT_PRODUCT)

    def test_failed_proof_without_failed_check_row_is_refused(self) -> None:
        payload = _proof_payload(
            checks=[{"check_id": "relevant_verification", "status": "not_run"}]
        )
        projection = project_repair_context(
            proof=payload,
            failure_class="product_failure",
            decisive_checks=(_FACT,),
        )
        self.assertFalse(projection.admitted)

    def test_admission_requires_safe_check_facts_not_just_failed_rows(self) -> None:
        # A failed proof row alone is not a fact: with no surviving decisive
        # check fact there is nothing observed to state, so the brief must
        # refuse instead of admitting an unobserved-check description.
        for decisive_checks in ((), ({"command": "", "exit_code": 1},)):
            with self.subTest(decisive_checks=decisive_checks):
                projection = project_repair_context(
                    proof=_proof_payload(),
                    failure_class="product_failure",
                    decisive_checks=decisive_checks,
                )
                self.assertFalse(projection.admitted)
                self.assertEqual(projection.refused_reason, REFUSED_NO_SAFE_CHECK_FACTS)
                self.assertIn(REFUSED_NO_SAFE_CHECK_FACTS, projection.warnings)
                payload = projection.to_payload()
                self.assertEqual(payload["admitted"], False)
                self.assertEqual(payload["refused_reason"], REFUSED_NO_SAFE_CHECK_FACTS)

    def test_fully_screened_decisive_command_is_refused_in_both_details(self) -> None:
        sensitive = DecisiveCheckFact(
            command="pytest -q --token api_key=sk-abcdefghijklmnop123456",
            cwd=".",
            exit_code=1,
        )
        for detail in ("full", DETAIL_MINIMAL):
            with self.subTest(detail=detail):
                projection = project_repair_context(
                    proof=_proof_payload(),
                    failure_class="product_failure",
                    decisive_checks=(sensitive,),
                    detail=detail,
                )
                self.assertFalse(projection.admitted)
                self.assertEqual(projection.refused_reason, REFUSED_NO_SAFE_CHECK_FACTS)

    def test_accepts_completion_proof_objects_not_only_mappings(self) -> None:
        from codey.completion.contract import (
            build_completion_contract,
            completion_check,
            project_completion_proof,
        )

        contract = build_completion_contract(
            domain="coding",
            subject_ref="run:x",
            checks=[
                completion_check(
                    "relevant_verification",
                    "fail",
                    "relevant_verification_failed",
                )
            ],
        )
        proof = project_completion_proof(contract)
        assert proof is not None
        projection = project_repair_context(
            proof=proof,
            failure_class="product_failure",
            decisive_checks=(_FACT,),
        )
        self.assertTrue(projection.admitted)
        self.assertEqual(projection.proof_id, proof.proof_id)


class RenderTests(unittest.TestCase):
    def test_prompt_contains_facts_but_no_fix_instructions(self) -> None:
        projection = project_repair_context(
            proof=_proof_payload(),
            failure_class="product_failure",
            decisive_checks=(_FACT,),
            changed_files=["src/foo.py"],
            analysis_run_refs=["analysis_run:" + "c" * 16],
        )
        text = projection.prompt_text
        self.assertIn("Failure class: product_failure", text)
        self.assertIn("Changed files: src/foo.py", text)
        self.assertIn("Exit: 1", text)
        self.assertIn("Refs:", text)
        lowered = text.casefold()
        for phrase in ("you should fix", "change line", "replace with", "suggested fix"):
            self.assertNotIn(phrase, lowered)

    def test_minimal_detail_is_deliberately_under_specified(self) -> None:
        full = project_repair_context(
            proof=_proof_payload(),
            failure_class="product_failure",
            decisive_checks=(_FACT,),
            changed_files=["src/foo.py"],
            analysis_run_refs=["analysis_run:" + "c" * 16],
        )
        minimal = project_repair_context(
            proof=_proof_payload(),
            failure_class="product_failure",
            decisive_checks=(_FACT,),
            changed_files=["src/foo.py"],
            detail=DETAIL_MINIMAL,
        )
        self.assertTrue(minimal.admitted)
        self.assertIn("Failing check: pytest -q (cwd .)", minimal.prompt_text)
        self.assertNotIn("Exit: 1", minimal.prompt_text)
        self.assertNotIn("Output tail", minimal.prompt_text)
        self.assertNotIn("Refs:", minimal.prompt_text)
        self.assertLess(len(minimal.prompt_text), len(full.prompt_text))

    def test_secret_lines_never_reach_the_prompt(self) -> None:
        leaky = DecisiveCheckFact(
            command="pytest -q",
            cwd=".",
            exit_code=1,
            result_summary=(
                "api_key=sk-abcdefghijklmnop123456\n"
                "FAILED tests/test_auth.py\n"
                "password=hunter2\n"
                "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk29\n"
                "1 failed"
            ),
        )
        projection = project_repair_context(
            proof=_proof_payload(),
            failure_class="product_failure",
            decisive_checks=(leaky,),
        )
        self.assertTrue(projection.admitted)
        self.assertNotIn("sk-abcdefghij", projection.prompt_text)
        self.assertNotIn("hunter2", projection.prompt_text)
        self.assertNotIn("Aa1Bb2", projection.prompt_text)
        self.assertIn("repair_output_line_screened", projection.warnings)

    def test_high_entropy_check_command_never_enters_the_prompt(self) -> None:
        # Marker-free random blobs in a command line are screened too. With
        # no safe decisive-check fact left, the whole projection refuses to
        # admit anything -- fail-closed, nothing reaches the model.
        blob = "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk29"
        fact = DecisiveCheckFact(
            command=f"python -c 'print(\"{blob}\")'",
            cwd=".",
            exit_code=1,
        )
        projection = project_repair_context(
            proof=_proof_payload(),
            failure_class="product_failure",
            decisive_checks=(fact,),
        )

        self.assertFalse(projection.admitted)
        self.assertEqual(projection.prompt_text, "")
        self.assertIn("repair_check_command_screened", projection.warnings)
        self.assertIn("refused_no_safe_check_facts", projection.warnings)

    def test_entropy_line_is_screened_while_safe_facts_still_admit(self) -> None:
        mixed = (
            DecisiveCheckFact(command="pytest -q", cwd=".", exit_code=1),
            DecisiveCheckFact(
                command="ruff check .",
                cwd=".",
                exit_code=1,
                result_summary=f"trace {('Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8Ii9Jj0Kk29')} end\nerror details",
            ),
        )
        projection = project_repair_context(
            proof=_proof_payload(),
            failure_class="product_failure",
            decisive_checks=mixed,
        )

        self.assertTrue(projection.admitted)
        self.assertNotIn("Aa1Bb2", projection.prompt_text)
        self.assertIn("error details", projection.prompt_text)

    def test_output_tail_is_bounded(self) -> None:
        big = DecisiveCheckFact(
            command="pytest -q",
            cwd=".",
            exit_code=1,
            result_summary="\n".join(f"line {i} of a very long failure log" for i in range(200)),
        )
        projection = project_repair_context(
            proof=_proof_payload(),
            failure_class="product_failure",
            decisive_checks=(big,),
        )
        self.assertLessEqual(len(projection.prompt_text), DEFAULT_REPAIR_CONTEXT_BUDGET_CHARS * 2)
        self.assertLessEqual(projection.summary_chars, 400)

    def test_budget_truncation_marks_the_projection_honest(self) -> None:
        many = tuple(
            DecisiveCheckFact(command=f"pytest -q suite{i}", cwd=".", exit_code=1)
            for i in range(20)
        )
        projection = project_repair_context(
            proof=_proof_payload(),
            failure_class="product_failure",
            decisive_checks=many,
            budget_chars=200,
        )
        self.assertTrue(projection.truncated)

    def test_managed_output_handle_never_enters_the_prompt(self) -> None:
        fact = DecisiveCheckFact(
            command="pytest -q",
            cwd=".",
            exit_code=1,
            managed_output_handle="out_abc123",
        )
        projection = project_repair_context(
            proof=_proof_payload(),
            failure_class="product_failure",
            decisive_checks=(fact,),
        )
        self.assertNotIn("out_abc123", projection.prompt_text)


class PayloadTests(unittest.TestCase):
    def test_payload_is_digest_only_with_no_raw_fields(self) -> None:
        projection = project_repair_context(
            proof=_proof_payload(),
            failure_class="product_failure",
            decisive_checks=(_FACT,),
            changed_files=["src/foo.py"],
        )
        payload = projection.to_payload()
        self.assertTrue(payload["admitted"])
        self.assertEqual(payload["context_source"], CONTEXT_SOURCE_KEY)
        self.assertTrue(str(payload["digest"]).startswith("sha256:"))
        banned_keys = {"prompt_text", "text", "summary", "stdout", "stderr", "output"}
        self.assertTrue(banned_keys.isdisjoint(payload))
        self.assertNotIn(_FACT.result_summary, str(payload))

    def test_digest_changes_when_facts_change(self) -> None:
        first = project_repair_context(
            proof=_proof_payload(),
            failure_class="product_failure",
            decisive_checks=(_FACT,),
        ).to_payload()["digest"]
        second = project_repair_context(
            proof=_proof_payload(reason_codes=["other_reason"]),
            failure_class="product_failure",
            decisive_checks=(_FACT,),
        ).to_payload()["digest"]
        third = project_repair_context(
            proof=_proof_payload(),
            failure_class="product_failure",
            decisive_checks=(_FACT,),
        ).to_payload()["digest"]
        self.assertNotEqual(first, second)
        self.assertEqual(first, third)

    def test_digest_changes_when_visible_brief_changes_with_same_metadata(self) -> None:
        first_fact = DecisiveCheckFact(
            command="pytest -q",
            cwd=".",
            exit_code=1,
            result_summary="FAILED tests/test_a.py - assert alpha",
        )
        second_fact = DecisiveCheckFact(
            command="pytest -q",
            cwd=".",
            exit_code=1,
            result_summary="FAILED tests/test_b.py - assert bravo",
        )
        first = project_repair_context(
            proof=_proof_payload(),
            failure_class="product_failure",
            decisive_checks=(first_fact,),
        ).to_payload()
        second = project_repair_context(
            proof=_proof_payload(),
            failure_class="product_failure",
            decisive_checks=(second_fact,),
        ).to_payload()

        self.assertNotEqual(first["digest"], second["digest"])
        self.assertNotIn("test_a", json.dumps(first))
        self.assertNotIn("test_b", json.dumps(second))

    def test_source_ref_and_round_budget_helpers(self) -> None:
        self.assertEqual(PROMPT_SOURCE_REF, "local_context:completion_repair_context")
        self.assertTrue(repair_candidate("failed", "product_failure"))
        self.assertFalse(repair_candidate("failed", "product_failure", repair_rounds=1))
        self.assertFalse(repair_candidate("failed", "environment_failure"))
        self.assertFalse(repair_candidate("complete", "product_failure"))


if __name__ == "__main__":
    unittest.main()
