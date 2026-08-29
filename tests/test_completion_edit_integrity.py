from __future__ import annotations

import difflib
import unittest

from codey.completion.edit_integrity import (
    EDIT_INTEGRITY_REASON_CODES,
    REASON_TEST_ASSERTIONS_REMOVED,
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
from tests.test_receipt import IMPORT_REMOVAL_DIFF


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
    def test_narrowed_verification_config_is_suspicious(self) -> None:
        removed_addopts = _diff(
            "[tool.pytest.ini_options]\naddopts = \"-q\"\n",
            "[tool.pytest.ini_options]\n",
            path="pyproject.toml",
        )
        added_deselect = _diff(
            "[tool.pytest.ini_options]\n",
            "[tool.pytest.ini_options]\naddopts = \"--deselect tests/test_mod.py\"\n",
            path="pyproject.toml",
        )

        removed_obs = _observe(diff=removed_addopts, paths=("pyproject.toml",))
        added_obs = _observe(diff=added_deselect, paths=("pyproject.toml",))

        self.assertIn(REASON_VERIFICATION_CONFIG_NARROWED, removed_obs.reason_codes)
        self.assertIn(REASON_VERIFICATION_CONFIG_NARROWED, added_obs.reason_codes)
        self.assertEqual(removed_obs.severity, SEVERITY_HIGH)


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


class _GreenDecision:
    """The decision shape the monitor consumes with a clean fresh pass."""

    def __init__(self) -> None:
        from codey.completion.verification import SOURCE_LOCAL_RUN, STANCE_FRESH_PASS, VerificationProvenance

        self.provenance = VerificationProvenance(STANCE_FRESH_PASS, SOURCE_LOCAL_RUN)
        self.analysis_run_refs = ()


if __name__ == "__main__":
    unittest.main()
