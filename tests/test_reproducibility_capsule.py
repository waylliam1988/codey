from __future__ import annotations

import re

from codey.research.analysis_run import analysis_run_record
from codey.research.artifact_lineage import artifact_ref_from_managed_output
from codey.research.reproducibility import (
    CAPSULE_STATUS_NO_RUNS,
    build_reproducibility_capsule,
)

_CAPSULE_RE = re.compile(r"^capsule:[0-9a-f]{16}$")


def _analysis_payload(**overrides: object) -> dict[str, object]:
    record = analysis_run_record({
        "run_id": "run-1",
        "tool_id": overrides.pop("tool_id", "1:0"),
        "tool_name": overrides.pop("tool_name", "run"),
        "command": overrides.pop("command", "pytest -q"),
        "cwd": ".",
        "project": "E:/codey",
        "exit_code": 0,
        "ok": True,
        **overrides,
    })
    assert record is not None
    return record.to_payload()


def _artifact_payload(**overrides: object) -> dict[str, object]:
    artifact = artifact_ref_from_managed_output({
        "handle": overrides.pop("handle", "out_0001_abc123def456"),
        "sha256": overrides.pop("sha256", "a" * 64),
        "stored_bytes": 40000,
        "stored_truncated": False,
        "origin_run_id": "run-1",
        "produced_by": "analysis_run:0123456789abcdef",
        **overrides,
    })
    assert artifact is not None
    return artifact.to_payload()


def test_capsule_aggregates_runs_and_artifacts() -> None:
    runs = [
        _analysis_payload(managed_output={
            "handle": "out_0001_abc123def456",
            "sha256": "a" * 64,
            "stored_truncated": False,
        }),
        _analysis_payload(command="python build.py", managed_output={
            "handle": "out_0002_1111222233334444",
            "sha256": "b" * 64,
            "stored_truncated": False,
        }),
    ]
    artifacts = [_artifact_payload()]
    capsule = build_reproducibility_capsule(
        run_id="run-1",
        analysis_runs=runs,
        artifacts=artifacts,
    )

    assert capsule is not None
    assert _CAPSULE_RE.fullmatch(capsule.capsule_id)
    assert len(capsule.analysis_run_refs) == 2
    assert len(capsule.artifact_refs) == 1
    assert capsule.reproduction_status == "output_captured"
    assert capsule.warnings == ()
    payload = capsule.to_payload()
    assert payload["run_id"] == "run-1"


def test_capsule_reports_mixed_capture_with_warning() -> None:
    capsule = build_reproducibility_capsule(
        run_id="run-1",
        analysis_runs=[
            _analysis_payload(managed_output={
                "handle": "out_0001_abc123def456",
                "sha256": "a" * 64,
                "stored_truncated": True,
            }),
            _analysis_payload(command="python other.py"),
        ],
    )

    assert capsule is not None
    assert capsule.reproduction_status == "output_not_captured"
    assert "mixed_output_capture" in capsule.warnings


def test_capsule_failed_run_dominates_status() -> None:
    capsule = build_reproducibility_capsule(
        run_id="run-1",
        analysis_runs=[
            _analysis_payload(ok=False, exit_code=1),
            _analysis_payload(command="python ok.py", managed_output={
                "handle": "out_0003_aaaaaaaaaaaaaaaa",
                "sha256": "c" * 64,
                "stored_truncated": False,
            }),
        ],
    )

    assert capsule is not None
    assert capsule.reproduction_status == "failed"


def test_capsule_without_runs_is_honest() -> None:
    capsule = build_reproducibility_capsule(run_id="run-1")

    assert capsule is not None
    assert capsule.reproduction_status == CAPSULE_STATUS_NO_RUNS
    assert capsule.analysis_run_refs == ()


def test_capsule_requires_run_identity() -> None:
    assert build_reproducibility_capsule(run_id="") is None


def test_capsule_id_is_stable_per_run() -> None:
    first = build_reproducibility_capsule(run_id="run-1")
    second = build_reproducibility_capsule(run_id="run-1", warnings=["extra"])
    third = build_reproducibility_capsule(run_id="run-2")

    assert first is not None and second is not None and third is not None
    assert first.capsule_id == second.capsule_id
    assert first.capsule_id != third.capsule_id
