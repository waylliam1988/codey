"""The Go-facing observation model preserves unknown latest verification facts."""

import pytest

from codey.runtime.core.verification_observation import (
    CompleteVerification,
    IncompleteVerification,
    decode_verification_observation,
    latest_verification_observations,
    verification_observations,
)

FINGERPRINT = "sha256:" + "a" * 64


def record(**changes):
    return {"command": "pytest", "cwd": "pkg", "revision": 3, "passed": True,
            "exit_code": 0, "workspace_revision": 7,
            "workspace_fingerprint": FINGERPRINT, **changes}


@pytest.mark.parametrize("changes,gap", [
    ({"exit_code": None}, "verification_result_missing"),
    ({"exit_code": False}, "verification_result_missing"),
    ({"workspace_revision": None}, "verification_identity_missing"),
    ({"workspace_revision": True}, "verification_identity_invalid"),
    ({"workspace_fingerprint": ""}, "verification_identity_missing"),
    ({"workspace_fingerprint": "invalid"}, "verification_identity_invalid"),
])
def test_latest_incomplete_observation_replaces_old_success(changes, gap):
    rows = verification_observations([record(), record(**changes)])
    assert isinstance(rows[0].outcome, CompleteVerification)
    latest = latest_verification_observations(rows, 3)["pytest", "pkg"]
    assert isinstance(latest.outcome, IncompleteVerification)
    assert latest.outcome.gap == gap


def test_latest_complete_failure_and_success_preserve_recorded_order():
    rows = verification_observations([record(), record(passed=False, exit_code=1)])
    assert latest_verification_observations(rows, 3)["pytest", "pkg"].outcome == CompleteVerification(
        1, 7, FINGERPRINT, False)
    assert latest_verification_observations((*rows, rows[0]), 3)["pytest", "pkg"].outcome == CompleteVerification(
        0, 7, FINGERPRINT, True)


@pytest.mark.parametrize("revision", [True, "3", 3.0])
def test_illegal_edit_revision_never_matches_real_edit_epoch(revision):
    rows = verification_observations([record(revision=revision)])
    assert rows[0].edit_revision is None
    assert latest_verification_observations(rows, 3) == {}


def test_passed_absence_and_explicit_false_have_different_meanings():
    raw = record()
    del raw["passed"]
    absent = decode_verification_observation(raw)
    explicit = decode_verification_observation(record(passed=False))
    assert absent.outcome == CompleteVerification(0, 7, FINGERPRINT, True)
    assert explicit.outcome == CompleteVerification(0, 7, FINGERPRINT, False)


def test_observations_are_detached_and_do_not_merge_commands_or_working_dirs():
    raw = record()
    rows = verification_observations([raw, record(cwd="other"), record(command="pytest -q")])
    raw["command"] = "changed"
    assert set(latest_verification_observations(rows, 3)) == {
        ("pytest", "pkg"), ("pytest", "other"), ("pytest -q", "pkg"),
    }
