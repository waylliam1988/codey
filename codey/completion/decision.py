"""The completion decision for one coding run (0.5.0).

``build_completion_decision`` is the single pure projection the TaskRunner
calls at each completion decision point. It combines the verification
facts this run observed -- tri-state freshness, explicit provenance, the
decisive analysis runs, and the hard-gate proof -- into one value, so the
agent loop never re-derives them inline and every consumer (repair
admission, receipt, trace) reads the same decision.

It is a projection leaf like ``completion.verification``: no I/O, no
models, no commands. It does not admit repairs and does not decide what
the receipt says; it only states what the local facts support.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from codey.completion.contract import CompletionProof
from codey.completion.verification import (
    VerificationProvenance,
    build_coding_completion_proof,
    classify_verification_failure,
    coding_verification_state,
    decisive_failure_fact,
    matching_analysis_run_refs,
    relevant_verification_pairs,
    verification_provenance,
)
from codey.runtime.execution_evidence import ExecutionEvidence


@dataclass(frozen=True)
class CompletionDecision:
    """One completion decision point's projected verification facts.

    ``proof`` is None exactly when the run is out of enforcement scope
    (not done, unchanged, or no changed files). ``failure_class`` is
    non-empty only for unsatisfied proofs.
    """

    proof: CompletionProof | None
    provenance: VerificationProvenance
    analysis_run_refs: tuple[str, ...]
    failure_class: str
    local_state: str


def build_completion_decision(
    *,
    run_id: str,
    stop_reason: str,
    task_changed: bool,
    files: tuple[str, ...],
    selected_check: object,
    evidence: ExecutionEvidence,
    analysis_run_payloads: Iterable[Mapping[str, object]] = (),
    project: str | Path | None = None,
    checkpoint_green: bool = False,
    verification_forbidden: bool = False,
    diagnostic_refs: tuple[str, ...] = (),
) -> CompletionDecision:
    """Project one coding run's verification facts into its decision.

    ``diagnostic_refs`` attach structured integrity diagnostics (for
    example an edit-integrity observation ref) to the proof, so the proof
    stays content-addressed over everything that qualifies it.
    """

    local_state = coding_verification_state(
        selected_check,
        evidence,
        files,
        root=project or ".",
    )
    provenance = verification_provenance(
        local_state=local_state,
        checkpoint_green=checkpoint_green,
    )
    analysis_refs = matching_analysis_run_refs(
        analysis_run_payloads,
        relevant_verification_pairs(
            local_state,
            selected_check,
            evidence,
            files,
            root=project or ".",
        ),
        project=project,
    )
    proof = build_coding_completion_proof(
        run_id=run_id,
        stop_reason=stop_reason,
        task_changed=task_changed,
        files=files,
        selected_check_present=selected_check is not None,
        provenance=provenance,
        analysis_run_refs=analysis_refs,
        verification_forbidden=verification_forbidden,
        diagnostic_refs=diagnostic_refs,
    )
    failure_class = ""
    if proof is not None and not proof.satisfied:
        decisive = decisive_failure_fact(
            selected_check,
            evidence,
            files,
            root=project or ".",
        )
        failure_class = classify_verification_failure(
            proof_status=proof.status,
            selected_check_present=selected_check is not None,
            decisive_error_code=str(getattr(decisive, "error_code", "") or ""),
            decisive_exit_code=getattr(decisive, "exit_code", None),
            decisive_result_summary=str(getattr(decisive, "result_summary", "") or ""),
        )
    return CompletionDecision(
        proof=proof,
        provenance=provenance,
        analysis_run_refs=analysis_refs,
        failure_class=failure_class,
        local_state=local_state,
    )


__all__ = [
    "CompletionDecision",
    "build_completion_decision",
]
