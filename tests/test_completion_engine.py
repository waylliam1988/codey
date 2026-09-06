from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from codey.completion.decision import CompletionDecision
from codey.completion.engine import CompletionEngine
from codey.completion.verification import VerificationProvenance
from codey.runtime.execution_evidence import ExecutionEvidence


@dataclass(frozen=True)
class _Integrity:
    diagnostic_refs: tuple[str, ...]


class _CountingCompletionEngine(CompletionEngine):
    def __init__(self) -> None:
        self.decision_calls = 0

    def _decision(self, **_kwargs) -> CompletionDecision:  # type: ignore[override]
        self.decision_calls += 1
        return CompletionDecision(
            proof=None,
            provenance=VerificationProvenance("unobserved", "none"),
            analysis_run_refs=(),
            failure_class="verification_unavailable",
            local_state="unobserved",
        )


def test_completion_engine_attaches_diagnostics_without_second_full_decision() -> None:
    engine = _CountingCompletionEngine()
    evidence = ExecutionEvidence(
        workspace_revision=1,
        workspace_fingerprint="sha256:" + "1" * 64,
    )

    with tempfile.TemporaryDirectory() as td, mock.patch(
        "codey.completion.engine.observe_edit_integrity",
        return_value=_Integrity(("edit_integrity:abc",)),
    ):
        result = engine.evaluate(
            run_id="run-1",
            task="fix app",
            changes={"diff": "diff --git a/app.py b/app.py\n"},
            stop_reason="done",
            task_changed=True,
            scope_files=("app.py",),
            selected_check=None,
            evidence=evidence,
            project=Path(td),
        )

    assert engine.decision_calls == 1
    assert result.decision.proof is not None
    assert result.decision.proof.diagnostic_refs == ("edit_integrity:abc",)
