"""Reproducibility Capsule aggregation over AnalysisRun and artifact facts.

A capsule is a bounded read-model snapshot: which analysis runs happened, what
outputs were captured, and whether the run is honestly reproducible from local
facts. v1 performs no re-execution; statuses only describe captured evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from codey.research.analysis_run import (
    CAPTURE_NOT_CAPTURED,
    CAPTURE_OUTPUT_CAPTURED,
    REPRODUCTION_FAILED,
)
from codey.utils.refs import stable_ref

CAPSULE_REF_PREFIX = "capsule:"
CAPSULE_STATUS_NO_RUNS = "no_analysis_runs"
MAX_ANALYSIS_RUN_REFS = 8
MAX_ARTIFACT_REFS = 8
MAX_WARNINGS = 8


@dataclass(frozen=True)
class ReproducibilityCapsule:
    capsule_id: str
    run_id: str
    analysis_run_refs: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    environment_digest: str
    reproduction_status: str
    warnings: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "capsule_id": self.capsule_id,
            "run_id": self.run_id,
            "analysis_run_refs": list(self.analysis_run_refs),
            "artifact_refs": list(self.artifact_refs),
            "environment_digest": self.environment_digest,
            "reproduction_status": self.reproduction_status,
            "warnings": list(self.warnings),
        }


def _aggregate_status(statuses: Iterable[str]) -> str:
    values = [str(item or "") for item in statuses]
    if not values:
        return CAPSULE_STATUS_NO_RUNS
    if REPRODUCTION_FAILED in values:
        return REPRODUCTION_FAILED
    if all(item == CAPTURE_OUTPUT_CAPTURED for item in values):
        return CAPTURE_OUTPUT_CAPTURED
    return CAPTURE_NOT_CAPTURED


def build_reproducibility_capsule(
    *,
    run_id: str,
    analysis_runs: Iterable[Mapping[str, object]] = (),
    artifacts: Iterable[Mapping[str, object]] = (),
    warnings: Iterable[object] = (),
) -> ReproducibilityCapsule | None:
    """Aggregate normalized payloads into one capsule snapshot.

    Accepts the ``to_payload()`` shapes of AnalysisRunRecord/ArtifactRef so the
    caller can feed accumulated trace-side dicts without keeping objects alive.
    Returns None when there is no run identity to bind the capsule to.
    """

    run_ref = str(run_id or "").strip()
    if not run_ref:
        return None

    run_refs: list[str] = []
    environment_digest = ""
    statuses: list[str] = []
    capsule_warnings = [str(item or "").strip() for item in warnings if str(item or "").strip()]
    for item in analysis_runs:
        if not isinstance(item, Mapping):
            continue
        ref = str(item.get("analysis_run_id") or "").strip()
        if not ref or ref in run_refs:
            continue
        run_refs.append(ref)
        status = str(item.get("reproduction_status") or "")
        if status:
            statuses.append(status)
        digest = str(item.get("environment_digest") or "")
        if not environment_digest and digest:
            environment_digest = digest
        if len(run_refs) >= MAX_ANALYSIS_RUN_REFS:
            break

    artifact_version_refs: list[str] = []
    for item in artifacts:
        if not isinstance(item, Mapping):
            continue
        version_id = str(item.get("version_id") or "").strip()
        if not version_id or version_id in artifact_version_refs:
            continue
        artifact_version_refs.append(version_id)
        if len(artifact_version_refs) >= MAX_ARTIFACT_REFS:
            break

    aggregate_warnings = list(dict.fromkeys(capsule_warnings))
    if statuses and CAPTURE_NOT_CAPTURED in statuses and CAPTURE_OUTPUT_CAPTURED in statuses:
        aggregate_warnings.append("mixed_output_capture")
    return ReproducibilityCapsule(
        capsule_id=stable_ref(CAPSULE_REF_PREFIX.removesuffix(":"), run_ref),
        run_id=run_ref[:120],
        analysis_run_refs=tuple(run_refs),
        artifact_refs=tuple(artifact_version_refs),
        environment_digest=environment_digest[:80],
        reproduction_status=_aggregate_status(statuses),
        warnings=tuple(aggregate_warnings[:MAX_WARNINGS]),
    )


__all__ = [
    "CAPSULE_REF_PREFIX",
    "CAPSULE_STATUS_NO_RUNS",
    "ReproducibilityCapsule",
    "build_reproducibility_capsule",
]
