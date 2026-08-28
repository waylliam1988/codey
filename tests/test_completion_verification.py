"""Pure coding verification facts and proof projection (0.4.13)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.completion.contract import (
    COMPLETION_BLOCKED,
    COMPLETION_COMPLETE,
    COMPLETION_COMPLETE_WITH_LIMITATIONS,
    COMPLETION_FAILED,
    CHECK_NOT_APPLICABLE,
    CHECK_NOT_RUN,
    CHECK_PASS,
)
from codey.completion.verification import (
    ENVIRONMENT_FAILURE_REASON_CODES,
    ENVIRONMENT_FAILURE_SIGNATURES,
    LIMITATION_DOCS_ONLY_CHANGE,
    LIMITATION_INHERITED_VERIFICATION,
    LIMITATION_VERIFICATION_FORBIDDEN_BY_USER,
    LIMITATION_VERIFICATION_NOT_LOCALLY_OBSERVED,
    SOURCE_CHECKPOINT,
    SOURCE_LOCAL_RUN,
    SOURCE_NONE,
    STANCE_FRESH_FAIL,
    STANCE_FRESH_PASS,
    STANCE_INHERITED_PASS,
    STANCE_UNVERIFIED,
    VERIFICATION_FRESH_FAIL,
    VERIFICATION_FRESH_PASS,
    VERIFICATION_UNOBSERVED,
    FAILURE_ENVIRONMENT,
    FAILURE_PRODUCT,
    FAILURE_UNKNOWN,
    FAILURE_VERIFICATION_UNAVAILABLE,
    VerificationProvenance,
    build_coding_completion_proof,
    classify_verification_failure,
    coding_completion_checks,
    coding_verification_state,
    decisive_failure_fact,
    match_environment_failure,
    matching_analysis_run_refs,
    relevant_verification_pairs,
    repairable_failure_class,
    verification_provenance,
)


def _check(command: str, cwd: str = ".", **extra):
    from codey.runtime.execution_evidence import CheckEvidence

    return CheckEvidence(command, cwd, **extra)


# Commands used in these tests carry no filesystem operands, so any real
# directory satisfies the shared canonicalizer; coverage matching itself is
# root-independent for exact-match candidates.
_ROOT = Path(tempfile.gettempdir())


def _selected():
    from codey.completion.verification_policy import VerificationCandidate

    return VerificationCandidate("pytest -q", ".", "successful run")


class _StubEvidence:
    """Minimal stand-in exposing only the facts the proof projection reads."""

    def __init__(
        self,
        successful=(),
        failed=(),
        observed_tool_events: int = 0,
    ) -> None:
        self.successful_checks = list(successful)
        self.failed_checks_after_edit = list(failed)
        self.observed_tool_events = observed_tool_events


class TriStateTests(unittest.TestCase):
    def test_verification_state_is_tri_state_not_event_count(self) -> None:
        files = ("src/mod.py",)
        selected = _selected()
        # Reads and searches are tool events too: they never make verification
        # fresh, and they must not turn into a fake failure either.
        self.assertEqual(
            coding_verification_state(
                selected,
                _StubEvidence(observed_tool_events=7),
                files,
                root=_ROOT,
            ),
            VERIFICATION_UNOBSERVED,
        )
        self.assertEqual(
            coding_verification_state(
                None,
                _StubEvidence(observed_tool_events=7),
                files,
                root=_ROOT,
            ),
            VERIFICATION_UNOBSERVED,
        )
        # A covering check that passed after the latest edit is fresh.
        self.assertEqual(
            coding_verification_state(
                selected,
                _StubEvidence(successful=[_check("pytest -q")], observed_tool_events=3),
                files,
                root=_ROOT,
            ),
            VERIFICATION_FRESH_PASS,
        )
        # A covering check that failed wins over any passing one.
        self.assertEqual(
            coding_verification_state(
                selected,
                _StubEvidence(
                    successful=[_check("ruff check .")],
                    failed=[_check("pytest -q")],
                    observed_tool_events=4,
                ),
                files,
                root=_ROOT,
            ),
            VERIFICATION_FRESH_FAIL,
        )

    def test_proof_only_cites_decisive_commands_not_every_executed_one(self) -> None:
        files = ("src/mod.py",)
        selected = _selected()
        evidence = _StubEvidence(
            successful=[
                _check("ruff check ."),
                _check("pytest -q"),
                _check("pytest -q", "packages/b"),
            ],
            failed=[],
            observed_tool_events=4,
        )
        # Only checks covering the selected candidate are decisive: a passing
        # run of the same command in a sibling package is real history but not
        # this proof's fact.
        pairs = relevant_verification_pairs(
            VERIFICATION_FRESH_PASS, selected, evidence, files, root=_ROOT
        )
        self.assertEqual(pairs, (("pytest -q", "."),))

        failing = _StubEvidence(
            successful=[_check("ruff check .")],
            failed=[_check("pytest -q")],
            observed_tool_events=5,
        )
        self.assertEqual(
            relevant_verification_pairs(
                VERIFICATION_FRESH_FAIL, selected, failing, files, root=_ROOT
            ),
            (("pytest -q", "."),),
        )
        self.assertEqual(
            relevant_verification_pairs(
                VERIFICATION_UNOBSERVED, selected, evidence, files, root=_ROOT
            ),
            (),
        )
        self.assertEqual(
            relevant_verification_pairs(
                VERIFICATION_FRESH_PASS, None, evidence, files, root=_ROOT
            ),
            (),
        )


class ProvenanceTests(unittest.TestCase):
    def test_provenance_precedence_local_fail_over_local_pass_over_inherited(self) -> None:
        resolved = verification_provenance(
            local_state=VERIFICATION_FRESH_FAIL,
            checkpoint_green=True,
        )
        self.assertEqual((resolved.stance, resolved.source), (STANCE_FRESH_FAIL, SOURCE_LOCAL_RUN))
        self.assertTrue(resolved.observed)
        self.assertFalse(resolved.clean_verification)

        fresh = verification_provenance(
            local_state=VERIFICATION_FRESH_PASS,
            checkpoint_green=False,
        )
        self.assertEqual((fresh.stance, fresh.source), (STANCE_FRESH_PASS, SOURCE_LOCAL_RUN))
        self.assertTrue(fresh.clean_verification)

        inherited = verification_provenance(
            local_state=VERIFICATION_UNOBSERVED,
            checkpoint_green=True,
        )
        self.assertEqual(
            (inherited.stance, inherited.source),
            (STANCE_INHERITED_PASS, SOURCE_CHECKPOINT),
        )
        self.assertFalse(inherited.observed)
        self.assertFalse(inherited.clean_verification)

        unverified = verification_provenance(
            local_state=VERIFICATION_UNOBSERVED,
            checkpoint_green=False,
        )
        self.assertEqual((unverified.stance, unverified.source), (STANCE_UNVERIFIED, SOURCE_NONE))

    def test_inherited_pass_is_a_limitation_never_clean_verification(self) -> None:
        provenance = verification_provenance(
            local_state=VERIFICATION_UNOBSERVED,
            checkpoint_green=True,
        )
        checks = coding_completion_checks(
            files=("src/mod.py",),
            selected_check_present=True,
            provenance=provenance,
        )
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0].status, CHECK_PASS)
        self.assertEqual(checks[0].reason_code, LIMITATION_INHERITED_VERIFICATION)

    def test_claimed_pass_without_observation_blocks_and_never_passes(self) -> None:
        # The 0.4.9 legacy debt, removed: a truthy reported value used to be
        # projected into a limitation-pass. The model's claim is not a fact;
        # unverified now blocks regardless of what the model reported.
        provenance = verification_provenance(
            local_state=VERIFICATION_UNOBSERVED,
            checkpoint_green=False,
        )
        checks = coding_completion_checks(
            files=("src/mod.py",),
            selected_check_present=True,
            provenance=provenance,
        )
        self.assertEqual(checks[0].status, CHECK_NOT_RUN)
        self.assertEqual(
            checks[0].reason_code,
            LIMITATION_VERIFICATION_NOT_LOCALLY_OBSERVED,
        )


class AnalysisRunRefTests(unittest.TestCase):
    def test_analysis_run_refs_attach_to_decisive_commands_with_cwd(self) -> None:
        from codey.research.identity import path_ref

        project = "E:/repo"
        analysis_runs = [
            {
                "analysis_run_id": "analysis_run:" + "a" * 16,
                "command_display": "pytest -q",
                "cwd_ref": path_ref(".", project=project),
                "ok": True,
            },
            {
                "analysis_run_id": "analysis_run:" + "b" * 16,
                "command_display": "pytest -q",
                # Same command, sibling package: must never be cited for root.
                "cwd_ref": path_ref("packages/b", project=project),
                "ok": False,
            },
            {
                "analysis_run_id": "analysis_run:" + "c" * 16,
                "command_display": "ruff check .",
                "cwd_ref": path_ref("packages/b", project=project),
                "ok": False,
            },
            {"analysis_run_id": "analysis_run:" + "d" * 16, "command_display": ""},
            {"analysis_run_id": "not-a-ref", "command_display": "pytest -q"},
            "junk",
        ]

        refs = matching_analysis_run_refs(analysis_runs, [("pytest -q", ".")], project=project)
        self.assertEqual(refs, ("analysis_run:" + "a" * 16,))

        # Same command under the other package cites the sibling's own run.
        self.assertEqual(
            matching_analysis_run_refs(
                analysis_runs,
                [("pytest -q", "packages/b")],
                project=project,
            ),
            ("analysis_run:" + "b" * 16,),
        )

        # Later payloads win for the same (command, cwd).
        newer = [*analysis_runs, {
            "analysis_run_id": "analysis_run:" + "e" * 16,
            "command_display": "pytest -q",
            "cwd_ref": path_ref(".", project=project),
            "ok": True,
        }]
        self.assertEqual(
            matching_analysis_run_refs(newer, [("pytest -q", ".")], project=project),
            ("analysis_run:" + "e" * 16,),
        )

        self.assertEqual(matching_analysis_run_refs(analysis_runs, [], project=project), ())
        self.assertEqual(matching_analysis_run_refs([], [("pytest -q", ".")], project=project), ())


class ProofTests(unittest.TestCase):
    def test_fresh_fail_proof_cites_the_failed_command_analysis_run(self) -> None:
        from codey.research.identity import path_ref

        class _SinkTrace:
            def __init__(self) -> None:
                self.proofs: list[dict] = []

            def record_completion_proof(self, payload) -> None:
                self.proofs.append(payload)

            def flush(self) -> None:
                return None

        project = "E:/repo"
        evidence = _StubEvidence(
            successful=[_check("ruff check .", "packages/a")],
            failed=[_check("pytest -q")],
            observed_tool_events=5,
        )
        analysis_runs = [{
            "analysis_run_id": "analysis_run:" + "b" * 16,
            "command_display": "pytest -q",
            "cwd_ref": path_ref(".", project=project),
            "ok": False,
        }, {
            "analysis_run_id": "analysis_run:" + "a" * 16,
            "command_display": "ruff check .",
            # The passing ruff run is not decisive for a fresh-fail state.
            "cwd_ref": path_ref("packages/a", project=project),
            "ok": True,
        }]
        state = coding_verification_state(_selected(), evidence, ("src/mod.py",), root=_ROOT)
        provenance = verification_provenance(local_state=state, checkpoint_green=False)
        refs = matching_analysis_run_refs(
            analysis_runs,
            relevant_verification_pairs(
                state, _selected(), evidence, ("src/mod.py",), root=_ROOT
            ),
            project=project,
        )
        proof = build_coding_completion_proof(
            run_id="run-fresh-fail",
            stop_reason="done",
            task_changed=True,
            files=("src/mod.py",),
            selected_check_present=True,
            provenance=provenance,
            analysis_run_refs=refs,
        )

        self.assertIsNotNone(proof)
        assert proof is not None
        payload = proof.to_payload()
        self.assertEqual(payload["status"], COMPLETION_FAILED)
        self.assertEqual(payload["blocked_reason"], "relevant_verification_failed")
        self.assertEqual(payload["analysis_run_refs"], ["analysis_run:" + "b" * 16])

    def test_fresh_pass_is_clean_complete(self) -> None:
        provenance = verification_provenance(
            local_state=VERIFICATION_FRESH_PASS,
            checkpoint_green=False,
        )
        proof = build_coding_completion_proof(
            run_id="run-clean",
            stop_reason="done",
            task_changed=True,
            files=("src/mod.py",),
            selected_check_present=True,
            provenance=provenance,
        )
        self.assertIsNotNone(proof)
        assert proof is not None
        self.assertEqual(proof.status, COMPLETION_COMPLETE)
        self.assertTrue(proof.satisfied)
        self.assertEqual(proof.limitation_refs, ())

    def test_docs_only_change_stays_complete_with_limitations(self) -> None:
        provenance = verification_provenance(
            local_state=VERIFICATION_UNOBSERVED,
            checkpoint_green=False,
        )
        proof = build_coding_completion_proof(
            run_id="run-docs",
            stop_reason="done",
            task_changed=True,
            files=("README.md",),
            selected_check_present=False,
            provenance=provenance,
        )
        self.assertIsNotNone(proof)
        assert proof is not None
        self.assertEqual(proof.status, COMPLETION_COMPLETE_WITH_LIMITATIONS)
        self.assertIn(LIMITATION_DOCS_ONLY_CHANGE, proof.limitation_refs)
        self.assertEqual(proof.checks[0].status, CHECK_NOT_APPLICABLE)

    def test_user_forbidden_verification_stays_complete_with_limitations(self) -> None:
        provenance = verification_provenance(
            local_state=VERIFICATION_UNOBSERVED,
            checkpoint_green=False,
        )
        proof = build_coding_completion_proof(
            run_id="run-no-checks",
            stop_reason="done",
            task_changed=True,
            files=("src/mod.py",),
            selected_check_present=True,
            provenance=provenance,
            verification_forbidden=True,
        )
        self.assertIsNotNone(proof)
        assert proof is not None
        self.assertEqual(proof.status, COMPLETION_COMPLETE_WITH_LIMITATIONS)
        self.assertFalse(proof.satisfied)
        self.assertEqual(
            proof.limitation_refs,
            (LIMITATION_VERIFICATION_FORBIDDEN_BY_USER,),
        )
        self.assertEqual(proof.checks[0].status, CHECK_NOT_APPLICABLE)
        self.assertEqual(
            proof.checks[0].reason_code,
            LIMITATION_VERIFICATION_FORBIDDEN_BY_USER,
        )

    def test_no_matching_candidate_blocks_done(self) -> None:
        provenance = verification_provenance(
            local_state=VERIFICATION_UNOBSERVED,
            checkpoint_green=False,
        )
        proof = build_coding_completion_proof(
            run_id="run-nocand",
            stop_reason="done",
            task_changed=True,
            files=("src/mod.py",),
            selected_check_present=False,
            provenance=provenance,
        )
        self.assertIsNotNone(proof)
        assert proof is not None
        self.assertEqual(proof.status, COMPLETION_BLOCKED)
        self.assertFalse(proof.satisfied)
        self.assertEqual(proof.blocked_reason, "no_matching_verification_command")

    def test_non_done_or_unchanged_runs_have_no_proof(self) -> None:
        provenance = verification_provenance(
            local_state=VERIFICATION_FRESH_PASS,
            checkpoint_green=False,
        )
        for kwargs in (
            {"stop_reason": "max_turns"},
            {"stop_reason": "done", "task_changed": False},
            {"stop_reason": "done", "task_changed": True, "files": ()},
        ):
            params = {
                "run_id": "run-x",
                "task_changed": True,
                "files": ("src/mod.py",),
                "selected_check_present": True,
                "provenance": provenance,
                **kwargs,
            }
            if "files" in kwargs and kwargs["files"] == ():
                params["selected_check_present"] = False
            self.assertIsNone(build_coding_completion_proof(**params))

    def test_decisive_failure_fact_returns_first_covering_failure(self) -> None:
        evidence = _StubEvidence(
            failed=[_check("ruff check ."), _check("pytest -q")],
        )
        decisive = decisive_failure_fact(_selected(), evidence, ("src/mod.py",), root=_ROOT)
        self.assertIsNotNone(decisive)
        assert decisive is not None
        self.assertEqual(decisive.command, "pytest -q")
        self.assertIsNone(decisive_failure_fact(None, evidence, ("src/mod.py",), root=_ROOT))


class FailureClassTests(unittest.TestCase):
    def test_classification_is_deterministic(self) -> None:
        self.assertEqual(
            classify_verification_failure(
                proof_status=COMPLETION_FAILED,
                selected_check_present=True,
                decisive_exit_code=1,
            ),
            FAILURE_PRODUCT,
        )
        self.assertEqual(
            classify_verification_failure(
                proof_status=COMPLETION_FAILED,
                selected_check_present=True,
                decisive_error_code="timeout",
                decisive_exit_code=None,
            ),
            FAILURE_ENVIRONMENT,
        )
        self.assertEqual(
            classify_verification_failure(proof_status=COMPLETION_BLOCKED),
            FAILURE_VERIFICATION_UNAVAILABLE,
        )
        self.assertEqual(
            classify_verification_failure(
                proof_status=COMPLETION_BLOCKED,
                provider_failed=True,
            ),
            "provider_failure",
        )
        self.assertEqual(
            classify_verification_failure(proof_status=COMPLETION_COMPLETE_WITH_LIMITATIONS),
            FAILURE_UNKNOWN,
        )

    def test_output_naming_the_environment_is_never_a_product_failure(self) -> None:
        environment_failures = (
            (
                "ERROR: No module named pytest\n1 exited with code 1",
                1,
            ),
            (
                "Traceback (most recent call last):\n"
                "ModuleNotFoundError: No module named 'redis'",
                1,
            ),
            ("./verify.sh: line 2: ruff: command not found", 127),
            ("'pytest' is not recognized as an internal or external command", 1),
            ("npm ERR! network request failed\nETIMEDOUT", 1),
            ("fatal: could not resolve host", 128),
            ("INTERNALERROR> traceback in pytest internals", 2),
            ("/bin/sh: segmentation fault (core dumped)", 139),
        )
        for summary, exit_code in environment_failures:
            self.assertEqual(
                classify_verification_failure(
                    proof_status=COMPLETION_FAILED,
                    selected_check_present=True,
                    decisive_exit_code=exit_code,
                    decisive_result_summary=summary,
                ),
                FAILURE_ENVIRONMENT,
                summary,
            )

    def test_assertion_failures_stay_product_even_alongside_noise(self) -> None:
        self.assertEqual(
            classify_verification_failure(
                proof_status=COMPLETION_FAILED,
                selected_check_present=True,
                decisive_exit_code=1,
                decisive_result_summary=(
                    "1 failed\nFAILED tests/test_mod.py - assert 1 == 2\n"
                    "1 failed in 0.4s"
                ),
            ),
            FAILURE_PRODUCT,
        )

    def test_assertions_quoting_environment_words_stay_product(self) -> None:
        # A product assertion that merely quotes an environment phrase
        # mid-sentence is a fixable product failure -- never environment.
        quoted_assertions = (
            "E       assert 'connection refused' == 'connected'",
            "E       AssertionError: cannot find module",
            ">       assert 'cannot find module' in stderr",
            'assert result.code == "ENOTFOUND"',
            "E   Failed: DID NOT RAISE ModuleNotFoundError",
            "expected ETIMEDOUT within 5s",
            "log mentioned internalerrors twice",
        )
        for summary in quoted_assertions:
            with self.subTest(summary=summary):
                self.assertEqual(
                    classify_verification_failure(
                        proof_status=COMPLETION_FAILED,
                        selected_check_present=True,
                        decisive_exit_code=1,
                        decisive_result_summary=summary,
                    ),
                    FAILURE_PRODUCT,
                )

    def test_environment_signal_matches_diagnostic_line_boundaries(self) -> None:
        diagnostics = (
            "./verify.sh: line 2: ruff: command not found",
            "bash: ruff: command not found",
            "Error: Cannot find module 'left-pad'",
            "'pytest' is not recognized as an internal or external command",
            "INTERNALERROR> traceback in pytest internals",
            "/bin/sh: segmentation fault (core dumped)",
        )
        for summary in diagnostics:
            with self.subTest(summary=summary):
                self.assertTrue(match_environment_failure(summary).matched)
        # Mid-line mentions stay unmatched by design -- including inside
        # longer genuine diagnostics; the safe misclassification direction
        # is one bounded repair attempt, never a hard block.
        self.assertFalse(match_environment_failure(
            "ssh: connect to host localhost port 22: Connection refused",
        ).matched)
        self.assertFalse(match_environment_failure(
            "E       assert 'connection refused' == 'connected'",
        ).matched)

    def test_match_names_reason_code_and_deciding_signature(self) -> None:
        cases = (
            (
                "Traceback (most recent call last):\n"
                "ModuleNotFoundError: No module named 'redis'",
                "missing_python_dependency",
                "modulenotfounderror",
            ),
            (
                "./verify.sh: line 2: ruff: command not found",
                "missing_executable_or_module",
                "command not found",
            ),
            (
                "ERROR: Could not find a version that satisfies the requirement redis",
                "unresolvable_package",
                "could not find a version that satisfies",
            ),
            ("npm ERR! network request failed\nETIMEDOUT", "network_unavailable", "etimedout"),
            ("INTERNALERROR> traceback in pytest internals", "test_infrastructure_crash", "internalerror"),
        )
        for summary, reason_code, signature in cases:
            with self.subTest(summary=summary):
                match = match_environment_failure(summary)
                self.assertTrue(match.matched)
                self.assertEqual(match.reason_code, reason_code)
                self.assertEqual(match.signature, signature)
        unmatched = match_environment_failure("assert 1 == 2")
        self.assertFalse(unmatched.matched)
        self.assertEqual(unmatched.reason_code, "")
        self.assertEqual(unmatched.signature, "")

    def test_environment_vocabulary_is_closed_and_case_insensitive(self) -> None:
        self.assertEqual(
            ENVIRONMENT_FAILURE_REASON_CODES,
            {
                "missing_python_dependency",
                "missing_executable_or_module",
                "unresolvable_package",
                "network_unavailable",
                "test_infrastructure_crash",
            },
        )
        prefixes = {
            prefix
            for group in ENVIRONMENT_FAILURE_SIGNATURES
            for prefix in group.prefixes
        }
        self.assertIn("no module named", prefixes)
        self.assertTrue(match_environment_failure("NO MODULE NAMED PYTEST").matched)
        self.assertFalse(match_environment_failure("", None, 0).matched)
        self.assertFalse(match_environment_failure("assert 1 == 2").matched)

    def test_only_product_failures_are_repair_candidates(self) -> None:
        self.assertTrue(repairable_failure_class(FAILURE_PRODUCT))
        self.assertFalse(repairable_failure_class(FAILURE_ENVIRONMENT))
        self.assertFalse(repairable_failure_class(FAILURE_VERIFICATION_UNAVAILABLE))
        self.assertFalse(repairable_failure_class(FAILURE_UNKNOWN))
        self.assertIsInstance(
            VerificationProvenance(STANCE_FRESH_PASS, SOURCE_LOCAL_RUN).to_payload(),
            dict,
        )


if __name__ == "__main__":
    unittest.main()
