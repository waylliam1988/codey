"""Completion proof and repair verdict projection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from codey.completion.decision import (
    CompletionDecision,
    build_completion_decision,
    completion_blocked_reason,
)
from codey.completion.edit_integrity import (
    EditIntegrityObservation,
    observe_edit_integrity,
)
from codey.completion.verification import build_coding_completion_proof
from codey.runtime.execution_evidence import ExecutionEvidence


COMPLETION_BLOCKED_NOTES = {
    "unobserved": (
        "Completion blocked: the required verification was never observed "
        "locally. Unobserved is not failure, but it is not done either."
    ),
    "max_repair_rounds": (
        "Completion blocked: local verification still failing after the "
        "repair round."
    ),
    "turn_budget_exhausted": (
        "Completion blocked: local verification still failing and no turn "
        "budget remains for a repair round."
    ),
    "environment_failure": (
        "Completion blocked: the verification command could not run "
        "(environment error), so the failure cannot be attributed to the "
        "code."
    ),
    "provider_failure": (
        "Completion blocked: provider failed during the repair phase."
    ),
    "repair_context_unavailable": (
        "Completion blocked: no safe bounded failure facts were available "
        "for a repair round."
    ),
    "repair_not_admitted": (
        "Completion blocked: local verification failed and this run "
        "admits no repair round."
    ),
}


@dataclass(frozen=True)
class CompletionEvidence:
    decision: CompletionDecision
    integrity: EditIntegrityObservation


def blocked_note(reason: str) -> str:
    return COMPLETION_BLOCKED_NOTES.get(
        reason,
        "Completion blocked: local completion proof did not pass.",
    )


class CompletionEngine:
    """Pure completion evaluation over already-collected local facts."""

    def evaluate(
        self,
        *,
        run_id: str,
        task: str,
        changes: object,
        stop_reason: str,
        task_changed: bool,
        scope_files: tuple[str, ...],
        selected_check: object,
        evidence: ExecutionEvidence,
        analysis_run_payloads: Iterable[Mapping[str, object]] = (),
        project: str | Path | None = None,
        checkpoint_green: bool = False,
        verification_forbidden: bool = False,
    ) -> CompletionEvidence:
        decision = self._decision(
            run_id=run_id,
            stop_reason=stop_reason,
            task_changed=task_changed,
            scope_files=scope_files,
            selected_check=selected_check,
            evidence=evidence,
            analysis_run_payloads=analysis_run_payloads,
            project=project,
            checkpoint_green=checkpoint_green,
            verification_forbidden=verification_forbidden,
        )
        integrity = observe_edit_integrity(
            task=task,
            changes=changes,
            diff=(
                str(changes.get("diff") or "")
                if isinstance(changes, dict)
                else ""
            ),
            files=scope_files,
            decision=decision,
            selected_check=selected_check,
            run_id=run_id,
        )
        if integrity.diagnostic_refs:
            decision = self._with_diagnostic_refs(
                decision,
                run_id=run_id,
                stop_reason=stop_reason,
                task_changed=task_changed,
                selected_check=selected_check,
                evidence=evidence,
                scope_files=scope_files,
                project=project,
                verification_forbidden=verification_forbidden,
                diagnostic_refs=integrity.diagnostic_refs,
            )
        return CompletionEvidence(decision=decision, integrity=integrity)

    @staticmethod
    def blocked_reason(
        *,
        proof_status: str,
        failure_class: str,
        remaining_turns: int,
        repair_rounds: int,
    ) -> str:
        return completion_blocked_reason(
            proof_status=proof_status,
            failure_class=failure_class,
            remaining_turns=remaining_turns,
            repair_rounds=repair_rounds,
        )

    @staticmethod
    def _decision(
        *,
        run_id: str,
        stop_reason: str,
        task_changed: bool,
        scope_files: tuple[str, ...],
        selected_check: object,
        evidence: ExecutionEvidence,
        analysis_run_payloads: Iterable[Mapping[str, object]],
        project: str | Path | None,
        checkpoint_green: bool,
        verification_forbidden: bool,
        diagnostic_refs: tuple[str, ...] = (),
    ) -> CompletionDecision:
        return build_completion_decision(
            run_id=run_id,
            stop_reason=stop_reason,
            task_changed=task_changed,
            files=scope_files,
            selected_check=selected_check,
            evidence=evidence,
            analysis_run_payloads=analysis_run_payloads,
            project=project,
            checkpoint_green=checkpoint_green,
            verification_forbidden=verification_forbidden,
            diagnostic_refs=diagnostic_refs,
        )

    @staticmethod
    def _with_diagnostic_refs(
        decision: CompletionDecision,
        *,
        run_id: str,
        stop_reason: str,
        task_changed: bool,
        selected_check: object,
        evidence: ExecutionEvidence,
        scope_files: tuple[str, ...],
        project: str | Path | None,
        verification_forbidden: bool,
        diagnostic_refs: tuple[str, ...],
    ) -> CompletionDecision:
        proof = build_coding_completion_proof(
            run_id=run_id,
            stop_reason=stop_reason,
            task_changed=task_changed,
            files=scope_files,
            selected_check_present=selected_check is not None,
            provenance=decision.provenance,
            analysis_run_refs=decision.analysis_run_refs,
            verification_forbidden=verification_forbidden,
            diagnostic_refs=diagnostic_refs,
            workspace_revision=getattr(evidence, "workspace_revision", 0),
            workspace_fingerprint=getattr(evidence, "workspace_fingerprint", ""),
            project=project,
        )
        return CompletionDecision(
            proof=proof,
            provenance=decision.provenance,
            analysis_run_refs=decision.analysis_run_refs,
            failure_class=decision.failure_class,
            local_state=decision.local_state,
        )
