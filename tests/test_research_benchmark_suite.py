from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.manual import research_benchmark_suite as validator


@pytest.fixture(scope="module")
def suite() -> validator.BenchmarkSuite:
    return validator.load_suite()


def test_bundled_suite_is_valid(suite: validator.BenchmarkSuite) -> None:
    assert validator.validate_suite(suite) == ()
    assert set(suite.development_case_ids()).isdisjoint(set(suite.held_out_case_ids()))
    assert set(suite.cases) == set(suite.development_case_ids()) | set(suite.held_out_case_ids())


def test_development_helper_never_returns_held_out_cases(
    suite: validator.BenchmarkSuite,
) -> None:
    dev = set(suite.development_case_ids())
    held = set(suite.held_out_case_ids())
    assert held and dev
    assert dev & held == set()
    for case_id in held:
        assert case_id not in dev
        expectations = suite.case_expectations(case_id)
        assert expectations, "held-out cases still carry expected observables"


def test_every_case_pins_at_least_one_boolean_gate_observable(
    suite: validator.BenchmarkSuite,
) -> None:
    from tools.research_benchmark.scorer import OBSERVABLE_NAMES

    for case_id in suite.cases:
        expectations = suite.case_expectations(case_id)
        assert expectations, case_id
        assert all(isinstance(value, bool) for value in expectations.values()), case_id
        assert set(expectations) <= OBSERVABLE_NAMES, case_id


def _copy_corpus(tmp_path: Path) -> Path:
    target = tmp_path / "research_benchmark"
    shutil.copytree(validator.FIXTURE_ROOT, target)
    return target


def test_tampered_case_fails_lock_hash(tmp_path: Path) -> None:
    root = _copy_corpus(tmp_path)
    case_path = root / "cases" / "stale_claim_refresh.json"
    payload = json.loads(case_path.read_text(encoding="utf-8"))
    payload["title"] = "tampered title"
    case_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    errors = validator.validate_suite(validator.load_suite(root))

    assert any("hash mismatch" in error for error in errors)


def test_unhashed_new_file_fails_lock(tmp_path: Path) -> None:
    root = _copy_corpus(tmp_path)
    (root / "files" / "extra.txt").write_text("new", encoding="utf-8")

    errors = validator.validate_suite(validator.load_suite(root))

    assert any("lock is missing hashes" in error for error in errors)


def test_update_lock_repins_corpus(tmp_path: Path) -> None:
    root = _copy_corpus(tmp_path)
    (root / "files" / "local_note.md").write_text(
        (root / "files" / "local_note.md").read_text(encoding="utf-8") + "\nappended\n",
        encoding="utf-8",
    )
    assert any("hash mismatch" in e for e in validator.validate_suite(validator.load_suite(root)))
    validator.update_lock(root)
    assert validator.validate_suite(validator.load_suite(root)) == ()


def test_fixture_paths_cannot_escape_the_suite_root(tmp_path: Path) -> None:
    root = _copy_corpus(tmp_path)
    case_path = root / "cases" / "stale_claim_refresh.json"
    payload = json.loads(case_path.read_text(encoding="utf-8"))
    payload["fixtures"] = ["../../../../pyproject.toml"]
    case_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    validator.update_lock(root)

    errors = validator.validate_suite(validator.load_suite(root))

    assert any("escapes suite root" in error for error in errors)


def test_unknown_expected_observable_names_are_rejected(tmp_path: Path) -> None:
    root = _copy_corpus(tmp_path)
    case_path = root / "cases" / "paper_progress_update.json"
    payload = json.loads(case_path.read_text(encoding="utf-8"))
    payload["expected_observables"]["raw_prompt_matched"] = True
    case_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    validator.update_lock(root)

    errors = validator.validate_suite(validator.load_suite(root))

    assert any("unknown expected observable" in error for error in errors)


def test_raw_material_keys_are_banned_inside_cases(tmp_path: Path) -> None:
    root = _copy_corpus(tmp_path)
    case_path = root / "cases" / "unsupported_claim_injection.json"
    payload = json.loads(case_path.read_text(encoding="utf-8"))
    payload["notes"] = [{"prompt": "ignore previous instructions"}]
    case_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    validator.update_lock(root)

    errors = validator.validate_suite(validator.load_suite(root))

    assert any("raw-material key" in error for error in errors)
    # The unknown-key fence catches the wrapper even without the deep scan.
    assert any("unknown keys" in error for error in errors)


def test_rubric_weights_and_vocabulary_are_validated(tmp_path: Path) -> None:
    root = _copy_corpus(tmp_path)
    rubric_path = root / "rubric.json"
    rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
    rubric["metrics"].append({
        "name": "broken",
        "observable": "not_a_real_observable",
        "weight": 5.0,
    })
    rubric["hard_gates"].append("made_up_gate")
    rubric_path.write_text(json.dumps(rubric, indent=2), encoding="utf-8")
    validator.update_lock(root)

    errors = validator.validate_suite(validator.load_suite(root))

    assert any("unknown observable" in error for error in errors)
    assert any("weights must sum to 1.0" in error for error in errors)
    assert any("not a gate criterion" in error for error in errors)


def test_cli_validates_bundled_suite() -> None:
    assert validator.main([]) == 0
