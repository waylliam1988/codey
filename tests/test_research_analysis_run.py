from __future__ import annotations

import re

from codey.research.analysis_run import (
    CAPTURE_NOT_CAPTURED,
    CAPTURE_OUTPUT_CAPTURED,
    REPRODUCTION_FAILED,
    analysis_run_record,
    environment_summary_digest,
)

_REF_RE = re.compile(r"^analysis_run:[0-9a-f]{16}$")


def _base_input(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "run_id": "run-1",
        "tool_id": "run",
        "command": "pytest -q tests/test_demo.py",
        "cwd": ".",
        "project": "E:/codey",
        "exit_code": 0,
        "ok": True,
        "started_at": "2026-08-22T08:00:00.000Z",
        "finished_at": "2026-08-22T08:00:01.500Z",
        "duration_ms": 1500,
        "managed_output": {},
    }
    data.update(overrides)
    return data


def test_analysis_run_record_projects_bounded_facts() -> None:
    record = analysis_run_record(_base_input())

    assert record is not None
    assert _REF_RE.fullmatch(record.analysis_run_id)
    assert record.command_digest.startswith("sha256:")
    assert record.command_display == "pytest -q tests/test_demo.py"
    assert record.cwd_ref["basename"] == "codey" or record.cwd_ref["basename"]
    assert record.exit_code == 0
    assert record.ok is True
    assert record.duration_ms == 1500
    assert record.managed_output_handle == ""
    assert record.capture_quality == CAPTURE_NOT_CAPTURED
    assert record.reproduction_status == CAPTURE_NOT_CAPTURED
    assert record.environment_digest.startswith("sha256:")
    assert record.warnings == ()

    payload = record.to_payload()
    assert payload["analysis_run_id"] == record.analysis_run_id
    assert isinstance(payload["cwd_ref"], dict)


def test_analysis_run_record_captured_output_is_reproducible() -> None:
    record = analysis_run_record(_base_input(
        managed_output={
            "handle": "out_0001_abc123def456",
            "original_bytes": 90000,
            "stored_bytes": 40000,
            "sha256": "a" * 64,
            "stored_truncated": True,
        },
    ))

    assert record is not None
    assert record.managed_output_handle == "out_0001_abc123def456"
    assert record.output_sha256 == "a" * 64
    assert record.stored_truncated is True
    assert record.capture_quality == CAPTURE_OUTPUT_CAPTURED
    assert record.reproduction_status == CAPTURE_OUTPUT_CAPTURED


def test_analysis_run_record_failed_command_reports_failure() -> None:
    record = analysis_run_record(_base_input(ok=False, exit_code=2))

    assert record is not None
    assert record.ok is False
    assert record.reproduction_status == REPRODUCTION_FAILED


def test_analysis_run_record_flags_missing_timing() -> None:
    record = analysis_run_record(_base_input(started_at="", finished_at="", duration_ms=None))

    assert record is not None
    assert record.warnings == ("timing_unavailable",)


def test_analysis_run_record_rejects_empty_command_and_bad_types() -> None:
    assert analysis_run_record(_base_input(command="   ")) is None
    assert analysis_run_record("not-a-mapping") is None  # type: ignore[arg-type]


def test_analysis_run_record_ignores_invalid_managed_sha() -> None:
    record = analysis_run_record(_base_input(managed_output={
        "handle": "out_0001_x",
        "sha256": "not-a-digest",
    }))

    assert record is not None
    assert record.output_sha256 == ""
    assert record.capture_quality == CAPTURE_NOT_CAPTURED
    assert "managed_output_sha_invalid" in record.warnings


def test_environment_summary_digest_is_stable_and_bounded() -> None:
    first = environment_summary_digest()
    second = environment_summary_digest()
    assert first == second
    assert first.startswith("sha256:")
