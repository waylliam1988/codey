from __future__ import annotations

import difflib
import unittest

from codey.completion.edit_integrity import (
    EDIT_INTEGRITY_REASON_CODES,
    REASON_TEST_ASSERTIONS_REMOVED,
    REASON_TEST_EXPECTED_EXCEPTION_WIDENED,
    REASON_TEST_EDIT_WITHOUT_PRODUCTION_CHANGE,
    REASON_TEST_IMPORT_GUARDED,
    REASON_TEST_IMPORT_REMOVED,
    REASON_TEST_SKIP_ADDED,
    REASON_UNAUTHORIZED_TEST_EDIT,
    REASON_VERIFICATION_CONFIG_NARROWED,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_NONE,
    STATUS_CLEAN,
    STATUS_MONITOR_ERROR,
    STATUS_SUSPICIOUS,
    STATUS_UNOBSERVED,
    EditIntegrityObservation,
    observe_edit_integrity,
)
from codey.research.evidence_runtime import is_valid_runtime_ref


_IMPORT_REMOVAL_OLD = "import redis  # noqa: F401\n\ndef test_value():\n"
_IMPORT_REMOVAL_NEW = "def test_value():\n"
IMPORT_REMOVAL_DIFF = "".join(difflib.unified_diff(
    _IMPORT_REMOVAL_OLD.splitlines(keepends=True),
    _IMPORT_REMOVAL_NEW.splitlines(keepends=True),
    fromfile="a/tests/test_mod.py",
    tofile="b/tests/test_mod.py",
))


def _diff(old: str, new: str, path: str = "tests/test_mod.py") -> str:
    body = "".join(difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    ))
    return f"diff --git a/{path} b/{path}\n{body}" if body else ""


def _observe(*, task: str = "Change src/mod.py VALUE from 1 to 2.", diff: str, paths, **kwargs):
    return observe_edit_integrity(
        task=task,
        changes={"changed_count": len(paths), "files": [{"path": p} for p in paths], "diff": diff},
        diff=diff,
        files=paths,
        **kwargs,
    )


class ImportIntegrityTests(unittest.TestCase):
    def test_deleted_import_is_high_suspicious(self) -> None:
        observation = _observe(diff=IMPORT_REMOVAL_DIFF, paths=("tests/test_mod.py",))

        self.assertEqual(observation.status, STATUS_SUSPICIOUS)
        self.assertEqual(observation.severity, SEVERITY_HIGH)
        self.assertIn(REASON_TEST_IMPORT_REMOVED, observation.reason_codes)
        self.assertEqual(observation.affected_paths, ("tests/test_mod.py",))

    def test_commented_and_guarded_imports_are_detected(self) -> None:
        old = "import redis  # noqa: F401\n\ndef test_value():\n    assert True\n"
        commented = _diff(old, "# import redis  # noqa: F401\n\ndef test_value():\n    assert True\n")
        guarded = _diff(
            old,
            "try:\n"
            "    import redis  # noqa: F401\n"
            "except ImportError:\n"
            "    pass\n"
            "\n"
            "def test_value():\n"
            "    assert True\n",
        )

        commented_obs = _observe(diff=commented, paths=("tests/test_mod.py",))
        guarded_obs = _observe(diff=guarded, paths=("tests/test_mod.py",))

        self.assertIn(REASON_TEST_IMPORT_REMOVED, commented_obs.reason_codes)
        self.assertIn(REASON_TEST_IMPORT_REMOVED, guarded_obs.reason_codes)
        self.assertIn(REASON_TEST_IMPORT_GUARDED, guarded_obs.reason_codes)

    def test_unrelated_removed_line_is_not_an_import_finding(self) -> None:
        diff = _diff("VALUE = 1\n", "VALUE = 2\n", path="src/mod.py")
        observation = _observe(diff=diff, paths=("src/mod.py",))

        self.assertEqual(observation.status, STATUS_CLEAN)
        self.assertEqual(observation.severity, SEVERITY_NONE)


class AssertionAndSkipTests(unittest.TestCase):
    def test_removed_assertion_is_suspicious(self) -> None:
        diff = _diff(
            "def test_value():\n    assert mod.VALUE == 2\n",
            "def test_value():\n    pass\n",
        )
        observation = _observe(diff=diff, paths=("tests/test_mod.py",))

        self.assertEqual(observation.severity, SEVERITY_HIGH)
        self.assertIn(REASON_TEST_ASSERTIONS_REMOVED, observation.reason_codes)

    def test_rewritten_assertion_is_not_an_assertion_finding(self) -> None:
        diff = _diff(
            "def test_value():\n    assert mod.VALUE == 1\n",
            "def test_value():\n    assert mod.VALUE == 2\n",
        )
        observation = _observe(diff=diff, paths=("tests/test_mod.py",))

        self.assertNotIn(REASON_TEST_ASSERTIONS_REMOVED, observation.reason_codes)

    def test_added_skip_is_suspicious(self) -> None:
        diff = _diff(
            "def test_value():\n    assert True\n",
            "import pytest\n\n\n@pytest.mark.skip(reason=\"later\")\ndef test_value():\n    assert True\n",
        )
        observation = _observe(diff=diff, paths=("tests/test_mod.py",))

        self.assertEqual(observation.severity, SEVERITY_HIGH)
        self.assertIn(REASON_TEST_SKIP_ADDED, observation.reason_codes)


class ConfigIntegrityTests(unittest.TestCase):
    def test_added_narrowing_flags_are_suspicious(self) -> None:
        added_deselect = _diff(
            "[tool.pytest.ini_options]\n",
            "[tool.pytest.ini_options]\naddopts = \"--deselect tests/test_mod.py\"\n",
            path="pyproject.toml",
        )
        added_ignore = _diff(
            "[tool.pytest.ini_options]\n",
            "[tool.pytest.ini_options]\naddopts = \"--ignore tests/test_mod.py\"\n",
            path="pyproject.toml",
        )
        added_k_not = _diff(
            "[tool.pytest.ini_options]\n",
            "[tool.pytest.ini_options]\naddopts = '-k not slow'\n",
            path="pyproject.toml",
        )

        for diff in (added_deselect, added_ignore, added_k_not):
            with self.subTest(line=diff.splitlines()[-1]):
                observation = _observe(diff=diff, paths=("pyproject.toml",))
                self.assertIn(REASON_VERIFICATION_CONFIG_NARROWED, observation.reason_codes)
                self.assertEqual(observation.severity, SEVERITY_HIGH)

    def test_restricted_testpaths_is_suspicious_but_widening_is_not(self) -> None:
        narrowed = _diff(
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            '[tool.pytest.ini_options]\ntestpaths = ["tests/unit"]\n',
            path="pyproject.toml",
        )
        widened = _diff(
            '[tool.pytest.ini_options]\ntestpaths = ["tests/unit"]\n',
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            path="pyproject.toml",
        )

        narrowed_obs = _observe(diff=narrowed, paths=("pyproject.toml",))
        widened_obs = _observe(diff=widened, paths=("pyproject.toml",))

        self.assertIn(REASON_VERIFICATION_CONFIG_NARROWED, narrowed_obs.reason_codes)
        self.assertEqual(narrowed_obs.severity, SEVERITY_HIGH)
        # Widening runs MORE tests, never fewer: not a narrowing signal.
        # (The low unauthorized-edit scope finding may still fire on a
        # protected-path-only change; it is a different code.)
        self.assertNotIn(REASON_VERIFICATION_CONFIG_NARROWED, widened_obs.reason_codes)

    def test_removing_verification_config_is_not_narrowing(self) -> None:
        removed = _diff(
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-q"\n',
            "[tool.pytest.ini_options]\n",
            path="pyproject.toml",
        )

        observation = _observe(diff=removed, paths=("pyproject.toml",))

        self.assertNotIn(REASON_VERIFICATION_CONFIG_NARROWED, observation.reason_codes)
        self.assertEqual(observation.severity, SEVERITY_LOW)


class ScopeIntegrityTests(unittest.TestCase):
    def test_green_verification_without_production_change_is_high(self) -> None:
        decision = _GreenDecision()

        observation = _observe(
            diff="",
            paths=("tests/test_mod.py",),
            decision=decision,
            selected_check=object(),
        )

        self.assertEqual(observation.severity, SEVERITY_HIGH)
        self.assertIn(REASON_TEST_EDIT_WITHOUT_PRODUCTION_CHANGE, observation.reason_codes)

    def test_mixed_production_and_test_edits_are_not_flagged_by_scope(self) -> None:
        observation = _observe(
            diff="",
            paths=("src/mod.py", "tests/test_mod.py"),
        )

        self.assertNotIn(REASON_UNAUTHORIZED_TEST_EDIT, observation.reason_codes)

    def test_unauthorized_test_edit_without_green_is_low(self) -> None:
        observation = _observe(diff="", paths=("tests/test_mod.py",))

        self.assertEqual(observation.status, STATUS_SUSPICIOUS)
        self.assertEqual(observation.severity, SEVERITY_LOW)
        self.assertIn(REASON_UNAUTHORIZED_TEST_EDIT, observation.reason_codes)

    def test_authorized_test_edits_are_downgraded_and_recorded(self) -> None:
        observation = _observe(
            task="Update the tests to expect the new value",
            diff=IMPORT_REMOVAL_DIFF,
            paths=("tests/test_mod.py",),
        )

        self.assertEqual(observation.status, STATUS_SUSPICIOUS)
        self.assertEqual(observation.severity, SEVERITY_LOW)
        self.assertTrue(observation.user_authorized_test_edit)
        for finding in observation.findings:
            self.assertEqual(finding.severity, SEVERITY_LOW)


class ImportNettingTests(unittest.TestCase):
    def test_moved_import_is_not_a_removal(self) -> None:
        # A legitimate reordering: the import leaves the top of the file
        # and re-appears unguarded elsewhere in the same section.
        diff = _diff(
            "import redis\n\ndef test_value():\n    assert True\n",
            "\ndef test_value():\n    import redis\n    assert True\n",
        )
        observation = _observe(diff=diff, paths=("tests/test_mod.py",))

        self.assertNotIn(REASON_TEST_IMPORT_REMOVED, observation.reason_codes)
        self.assertNotIn(REASON_TEST_IMPORT_GUARDED, observation.reason_codes)

    def test_readdition_inside_import_guard_does_not_cancel_removal(self) -> None:
        diff = _diff(
            "import redis\n\ndef test_value():\n    assert True\n",
            "try:\n    import redis\nexcept ImportError:\n    pass\n\ndef test_value():\n    assert True\n",
        )
        observation = _observe(diff=diff, paths=("tests/test_mod.py",))

        self.assertIn(REASON_TEST_IMPORT_REMOVED, observation.reason_codes)
        self.assertIn(REASON_TEST_IMPORT_GUARDED, observation.reason_codes)


class AssertionWeakeningTests(unittest.TestCase):
    def test_with_pytest_raises_removal_is_counted(self) -> None:
        diff = _diff(
            "def test_value():\n    with pytest.raises(ValueError):\n        mod.load()\n",
            "def test_value():\n    mod.load()\n",
        )
        observation = _observe(diff=diff, paths=("tests/test_mod.py",))

        self.assertEqual(observation.severity, SEVERITY_HIGH)
        self.assertIn(REASON_TEST_ASSERTIONS_REMOVED, observation.reason_codes)

    def test_specific_exception_widened_to_exception_is_suspicious(self) -> None:
        diff = _diff(
            "def test_value():\n    with pytest.raises(ValueError):\n        mod.load()\n",
            "def test_value():\n    with pytest.raises(Exception):\n        mod.load()\n",
        )
        observation = _observe(diff=diff, paths=("tests/test_mod.py",))

        self.assertEqual(observation.status, STATUS_SUSPICIOUS)
        self.assertIn(REASON_TEST_EXPECTED_EXCEPTION_WIDENED, observation.reason_codes)
        self.assertEqual(observation.severity, SEVERITY_HIGH)

    def test_specific_exception_swap_is_not_widening(self) -> None:
        diff = _diff(
            "def test_value():\n    with pytest.raises(ValueError):\n        mod.load()\n",
            "def test_value():\n    with pytest.raises(KeyError):\n        mod.load()\n",
        )
        observation = _observe(diff=diff, paths=("tests/test_mod.py",))

        self.assertNotIn(REASON_TEST_EXPECTED_EXCEPTION_WIDENED, observation.reason_codes)


class ScanSaturationTests(unittest.TestCase):
    def test_huge_production_diff_does_not_hide_test_section(self) -> None:
        # A production file saturates its section cap; the tampered test
        # file after it must still be observed.
        filler = "".join("-line %d\n+line %dx\n" % (i, i) for i in range(2400))
        big_first = (
            "diff --git a/src/big.py b/src/big.py\n"
            "--- a/src/big.py\n"
            "+++ b/src/big.py\n"
            "@@ -1,2400 +1,2400 @@\n"
            + filler
            + _diff(
                "import redis\n\ndef test_value():\n    assert True\n",
                "def test_value():\n    assert True\n",
            )
        )
        observation = _observe(
            diff=big_first,
            paths=("src/big.py", "tests/test_mod.py"),
        )

        self.assertEqual(observation.status, STATUS_SUSPICIOUS)
        self.assertEqual(observation.severity, SEVERITY_HIGH)
        self.assertIn(REASON_TEST_IMPORT_REMOVED, observation.reason_codes)


class NodeVerificationConfigTests(unittest.TestCase):
    def test_gutted_npm_test_script_is_suspicious(self) -> None:
        gutted = _diff(
            '"scripts": {\n  "test": "vitest run"\n}\n',
            '"scripts": {\n  "test": "echo no tests"\n}\n',
            path="package.json",
        )
        legit_dependency = _diff(
            '"dependencies": {\n}\n',
            '"dependencies": {\n  "left-pad": "1.0.0"\n}\n',
            path="package.json",
        )

        gutted_obs = _observe(diff=gutted, paths=("package.json",))
        legit_obs = _observe(diff=legit_dependency, paths=("package.json",))

        self.assertIn(REASON_VERIFICATION_CONFIG_NARROWED, gutted_obs.reason_codes)
        self.assertEqual(gutted_obs.severity, SEVERITY_HIGH)
        self.assertEqual(legit_obs.status, STATUS_CLEAN)

    def test_swapped_runner_is_not_gutting(self) -> None:
        swapped = _diff(
            '"scripts": {\n  "test": "jest"\n}\n',
            '"scripts": {\n  "test": "vitest run"\n}\n',
            path="package.json",
        )
        observation = _observe(diff=swapped, paths=("package.json",))

        self.assertNotIn(REASON_VERIFICATION_CONFIG_NARROWED, observation.reason_codes)

    def test_narrowing_flag_in_test_script_is_suspicious(self) -> None:
        diff = _diff(
            '"scripts": {\n  "test": "jest"\n}\n',
            '"scripts": {\n  "test": "jest --testPathIgnorePatterns=src/"\n}\n',
            path="package.json",
        )
        observation = _observe(diff=diff, paths=("package.json",))

        self.assertIn(REASON_VERIFICATION_CONFIG_NARROWED, observation.reason_codes)
        self.assertEqual(observation.severity, SEVERITY_HIGH)

    def test_jest_config_narrowing_flag_is_suspicious(self) -> None:
        diff = _diff(
            "export default {\n};\n",
            "export default {\n  testPathIgnorePatterns: ['src/'],\n};\n",
            path="jest.config.ts",
        )
        observation = _observe(diff=diff, paths=("jest.config.ts",))

        self.assertIn(REASON_VERIFICATION_CONFIG_NARROWED, observation.reason_codes)
        self.assertEqual(observation.severity, SEVERITY_HIGH)


class MonitorContractTests(unittest.TestCase):
    def test_no_changed_paths_is_unobserved(self) -> None:
        observation = _observe(diff="", paths=())

        self.assertEqual(observation.status, STATUS_UNOBSERVED)
        self.assertEqual(observation.diagnostic_refs, ())

    def test_monitor_exception_is_never_clean(self) -> None:
        class Exploding:
            def __iter__(self):
                raise RuntimeError("monitor exploded")

        observation = observe_edit_integrity(
            task="fix",
            changes={},
            diff="",
            files=Exploding(),
        )

        self.assertEqual(observation.status, STATUS_MONITOR_ERROR)
        self.assertIn("monitor_error", observation.reason_codes)
        self.assertTrue(observation.monitor_error_ref)
        self.assertTrue(observation.diagnostic_refs)

    def test_clean_observation_has_no_diagnostic_refs(self) -> None:
        diff = _diff("VALUE = 1\n", "VALUE = 2\n", path="src/mod.py")
        observation = _observe(diff=diff, paths=("src/mod.py",))

        self.assertEqual(observation.status, STATUS_CLEAN)
        self.assertEqual(observation.diagnostic_refs, ())

    def test_suspicious_observation_carries_a_valid_integrity_ref(self) -> None:
        observation = _observe(diff=IMPORT_REMOVAL_DIFF, paths=("tests/test_mod.py",))

        self.assertTrue(observation.diagnostic_refs)
        self.assertTrue(
            is_valid_runtime_ref(observation.diagnostic_refs[0], kinds=("edit_integrity",))
        )
        for finding in observation.findings:
            self.assertTrue(
                is_valid_runtime_ref(
                    finding.finding_ref,
                    kinds=("edit_integrity_finding",),
                )
            )

    def test_findings_carry_no_diff_text_and_reason_codes_are_closed(self) -> None:
        observation = _observe(
            diff=IMPORT_REMOVAL_DIFF + _diff(
                "[tool.pytest.ini_options]\naddopts = \"-q\"\n",
                "[tool.pytest.ini_options]\n",
                path="pyproject.toml",
            ),
            paths=("tests/test_mod.py", "pyproject.toml"),
        )

        self.assertNotIn("\n", str(observation.to_payload()))
        self.assertNotIn("import redis\n", str(observation.to_payload()))
        for finding in observation.findings:
            self.assertIn(finding.reason_code, EDIT_INTEGRITY_REASON_CODES)

    def test_observation_to_payload_round_trips_schema(self) -> None:
        observation = _observe(diff=IMPORT_REMOVAL_DIFF, paths=("tests/test_mod.py",))
        payload = observation.to_payload()

        self.assertEqual(payload["schema_version"], observation.schema_version)
        self.assertEqual(payload["status"], STATUS_SUSPICIOUS)
        self.assertEqual(payload["severity"], SEVERITY_HIGH)
        self.assertIsInstance(observation, EditIntegrityObservation)


class _SatisfiedProof:
    """Proof-shaped stub: the receipt reads status and content ids."""

    status = "complete"
    proof_id = "completion_proof:" + "a" * 16
    contract_id = "completion_contract:" + "b" * 16


class _GreenDecision:
    """The decision shape the monitor and receipt consume: a clean fresh
    pass backed by a satisfied proof."""

    def __init__(self) -> None:
        from codey.completion.verification import SOURCE_LOCAL_RUN, STANCE_FRESH_PASS, VerificationProvenance

        self.provenance = VerificationProvenance(STANCE_FRESH_PASS, SOURCE_LOCAL_RUN)
        self.analysis_run_refs = ()
        self.proof = _SatisfiedProof()


if __name__ == "__main__":
    unittest.main()
