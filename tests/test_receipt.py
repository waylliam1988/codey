from __future__ import annotations

import unittest

from codey.completion.edit_integrity import (
    SEVERITY_HIGH,
    SEVERITY_LOW,
    STATUS_MONITOR_ERROR,
    STATUS_SUSPICIOUS,
    EditIntegrityObservation,
    observe_edit_integrity,
)
from codey.runs.receipt import (
    RECEIPT_SCHEMA_VERSION,
    VERIFICATION_TRUST_LIMITED,
    VERIFICATION_TRUST_NEEDS_REVIEW,
    VERIFICATION_TRUST_TRUSTED,
    build_task_receipt,
    task_receipt_from_payload,
)
from tests.test_completion_edit_integrity import IMPORT_REMOVAL_DIFF, _GreenDecision


class TaskReceiptTests(unittest.TestCase):
    def test_no_changes_receipt_is_short(self) -> None:
        receipt = build_task_receipt({"mode": "snapshot", "changed_count": 0})

        self.assertEqual(receipt.display.summary, "No files changed")
        self.assertEqual(receipt.work.changed_count, 0)
        self.assertFalse(receipt.work.restore_available)
        self.assertEqual(receipt.verification.trust, VERIFICATION_TRUST_LIMITED)
        self.assertEqual(receipt.schema_version, RECEIPT_SCHEMA_VERSION)

    def test_trusted_pass_receipt_names_checks_only(self) -> None:
        observation = observe_edit_integrity(
            task="Change src/mod.py VALUE from 1 to 2.",
            changes={"changed_count": 2, "files": [{"path": "src/mod.py"}], "diff": ""},
            diff="",
            files=("src/mod.py",),
            decision=None,
            run_id="run-1",
        )
        receipt = build_task_receipt(
            {"mode": "snapshot", "changed_count": 2},
            integrity=observation,
            checks_passed=True,
        )

        self.assertEqual(receipt.display.summary, "2 files changed · checks passed")
        self.assertEqual(receipt.display.detail, "")
        self.assertTrue(receipt.work.restore_available)
        self.assertTrue(receipt.verification.checks_passed)
        self.assertEqual(receipt.verification.trust, VERIFICATION_TRUST_TRUSTED)
        self.assertEqual(receipt.to_dict()["schema_version"], RECEIPT_SCHEMA_VERSION)

    def test_unwatched_green_is_limited_by_contract(self) -> None:
        # A receipt claiming passing checks without any integrity
        # observation cannot be trusted: nobody watched for tampering.
        receipt = build_task_receipt(
            {"mode": "git", "changed_count": 2},
            checks_passed=True,
        )

        self.assertEqual(receipt.verification.trust, VERIFICATION_TRUST_LIMITED)
        self.assertEqual(receipt.display.summary, "2 files changed · verification limited")
        self.assertEqual(receipt.display.detail, "Verification monitoring failed")

    def test_git_changes_do_not_claim_snapshot_restore(self) -> None:
        observation = observe_edit_integrity(
            task="Change src/mod.py VALUE from 1 to 2.",
            changes={"changed_count": 1, "files": [{"path": "src/mod.py"}], "diff": ""},
            diff="",
            files=("src/mod.py",),
            decision=None,
            run_id="run-1",
        )
        receipt = build_task_receipt(
            {"mode": "git", "changed_count": 1},
            integrity=observation,
            checks_passed=True,
        )

        self.assertEqual(receipt.display.summary, "1 file changed · checks passed")
        self.assertFalse(receipt.work.restore_available)

    def test_high_suspicious_receipt_needs_review(self) -> None:
        integrity = observe_edit_integrity(
            task="fix src/mod.py",
            changes={"changed_count": 1, "files": [{"path": "tests/test_mod.py"}], "diff": IMPORT_REMOVAL_DIFF},
            diff=IMPORT_REMOVAL_DIFF,
            files=("tests/test_mod.py",),
            decision=None,
            run_id="run-1",
        )
        receipt = build_task_receipt(
            {"mode": "git", "changed_count": 2},
            integrity=integrity,
            checks_passed=True,
        )

        self.assertEqual(integrity.status, STATUS_SUSPICIOUS)
        self.assertEqual(integrity.severity, SEVERITY_HIGH)
        self.assertEqual(receipt.display.summary, "2 files changed · checks need review")
        self.assertEqual(receipt.display.detail, "Test changes may have weakened verification")
        self.assertEqual(receipt.verification.trust, VERIFICATION_TRUST_NEEDS_REVIEW)
        self.assertEqual(receipt.integrity.status, STATUS_SUSPICIOUS)
        self.assertEqual(receipt.integrity.severity, SEVERITY_HIGH)
        self.assertIn("test_import_removed_or_commented", receipt.integrity.reason_codes)
        # The audit loop closes inside the receipt: paths and refs travel
        # with it, Details and headless never have to guess from the trace.
        self.assertEqual(receipt.integrity.affected_paths, ("tests/test_mod.py",))
        self.assertEqual(receipt.integrity.refs[0], integrity.observation_ref)
        self.assertFalse(receipt.integrity.authorized_test_edit)

    def test_authorized_low_suspicious_receipt_stays_trusted(self) -> None:
        integrity = observe_edit_integrity(
            task="update the tests to expect the new value",
            changes={"changed_count": 1, "files": [{"path": "tests/test_mod.py"}], "diff": IMPORT_REMOVAL_DIFF},
            diff=IMPORT_REMOVAL_DIFF,
            files=("tests/test_mod.py",),
            decision=None,
            run_id="run-1",
        )
        receipt = build_task_receipt(
            {"mode": "git", "changed_count": 1},
            integrity=integrity,
            checks_passed=True,
        )

        self.assertEqual(integrity.severity, SEVERITY_LOW)
        self.assertTrue(integrity.user_authorized_test_edit)
        self.assertEqual(receipt.verification.trust, VERIFICATION_TRUST_TRUSTED)
        self.assertEqual(receipt.display.summary, "1 file changed · checks passed")

    def test_receipt_carries_proof_state_and_refs_from_decision(self) -> None:
        observation = observe_edit_integrity(
            task="Change src/mod.py VALUE from 1 to 2.",
            changes={"changed_count": 1, "files": [{"path": "src/mod.py"}], "diff": ""},
            diff="",
            files=("src/mod.py",),
            decision=None,
            run_id="run-1",
        )
        receipt = build_task_receipt(
            {"mode": "git", "changed_count": 1},
            decision=_GreenDecision(),
            integrity=observation,
            checks_passed=True,
        )

        self.assertEqual(receipt.verification.state, "complete")
        self.assertEqual(receipt.verification.stance, "fresh_pass")
        self.assertEqual(receipt.verification.source, "local_run")
        self.assertEqual(len(receipt.verification.proof_refs), 2)
        self.assertTrue(
            receipt.verification.proof_refs[0].startswith("completion_proof:"),
        )
        self.assertTrue(
            receipt.verification.proof_refs[1].startswith("completion_contract:"),
        )

    def test_monitor_error_receipt_is_limited(self) -> None:
        integrity = EditIntegrityObservation(
            schema_version=1,
            run_id="run-1",
            status=STATUS_MONITOR_ERROR,
            severity="none",
            reason_codes=("monitor_error",),
            findings=(),
            user_authorized_test_edit=False,
            affected_paths=(),
            verification_refs=(),
            change_refs=(),
            observation_ref="edit_integrity:0123456789abcdef",
            monitor_error_ref="sha256:0123456789abcdef",
        )
        receipt = build_task_receipt(
            {"mode": "git", "changed_count": 2},
            integrity=integrity,
            checks_passed=True,
        )

        self.assertEqual(receipt.verification.trust, VERIFICATION_TRUST_LIMITED)
        self.assertEqual(receipt.display.summary, "2 files changed · verification limited")
        self.assertEqual(receipt.display.detail, "Verification monitoring failed")

    def test_failed_checks_receipt_makes_no_verification_claim(self) -> None:
        receipt = build_task_receipt({"mode": "git", "changed_count": 3})

        self.assertEqual(receipt.display.summary, "3 files changed")
        self.assertEqual(receipt.verification.trust, VERIFICATION_TRUST_LIMITED)
        self.assertFalse(receipt.verification.checks_passed)

    def test_receipt_payload_round_trip_and_fail_closed(self) -> None:
        integrity = observe_edit_integrity(
            task="fix src/mod.py",
            changes={"changed_count": 1, "files": [{"path": "tests/test_mod.py"}], "diff": IMPORT_REMOVAL_DIFF},
            diff=IMPORT_REMOVAL_DIFF,
            files=("tests/test_mod.py",),
            decision=_GreenDecision(),
            run_id="run-1",
        )
        receipt = build_task_receipt(
            {"mode": "snapshot", "changed_count": 2},
            decision=_GreenDecision(),
            integrity=integrity,
            checks_passed=True,
        )
        restored = task_receipt_from_payload(receipt.to_dict())

        self.assertIsNotNone(restored)
        self.assertEqual(restored, receipt)
        self.assertIsNone(task_receipt_from_payload({"text": "legacy"}))
        self.assertIsNone(task_receipt_from_payload(None))
        legacy = receipt.to_dict()
        legacy["schema_version"] = 0
        self.assertIsNone(task_receipt_from_payload(legacy))
        broken = receipt.to_dict()
        broken["verification"]["trust"] = "definitely_fine"
        self.assertIsNone(task_receipt_from_payload(broken))


if __name__ == "__main__":
    unittest.main()
