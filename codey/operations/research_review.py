"""Explicit bounded diagnostics through the shared driver; no publication authority."""

from __future__ import annotations

from typing import Any

from codey.knowledge.model import canonical_json
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
)
from codey.operations.research_continuation import ResearchInvocation
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.pipeline import ResearchContinuationError
from codey.research.review_batches import batch_receipts, batch_scopes, merge_assessments, next_batch
from codey.research.semantic_review import (
    parse_review,
    review_basis,
    review_issues,
    submitted_review,
)
from codey.runtime.core.cancellation import TaskCancelled
from codey.runtime.core.models import ToolCall, ToolResult


def assess_report(history: Any, result: Any, tools: Any, *, provider: Any, provider_id: str,
                  cycle: int, on_event: Any, stop_flag: Any, parent_policy: Any) -> dict[str, Any]:
    basis = review_basis(result.accepted_report, tools, result.question)
    prompt = ("Assess the frozen research report using the current batch supplied by the driver. "
              "Original assessment identity: " + basis["basis_digest"])
    invocation = ResearchInvocation.assessment(cycle)
    state = history.state()
    if state.writer_attempt < invocation.attempt:
        provider.new_chat()
    sink = KernelEffectSink(history.mutations, session_id=history.session_id, run_id=history.run_id,
        provider_id=provider_id, phase="research", managed_outputs=history.outputs, durable=history.durable)
    grants = frozenset(grant for grant in ("control", "knowledge.write") if parent_policy.allows(grant))
    session = TaskSession(policy=TaskPolicy(grants), task_kind="research_review",
                          max_turns=min(3, len(batch_scopes(basis))), task_text=prompt, coding_context_enabled=False,
                          research_review_basis=basis)
    executor = _submission_executor(history, session, basis, provider_id=provider_id, cycle=cycle)
    outcome = history.kernel(session, invocation=invocation, request=KernelRunRequest(
        task_guidance="Assess only the frozen research basis. Complete the assessment even when the draft fails.",
        transport=KernelTransportDeps(provider=provider, run_id=history.run_id, provider_id=provider_id,
            user_task=prompt, effect_scope=f"research:review:{cycle}", stop_flag=stop_flag),
        execution=KernelExecutionDeps(executors={"submit_research_review": executor},
                                       managed_outputs=history.outputs, session_id=history.session_id,
                                       durable=history.durable),
        observation=KernelObservationDeps(intent_sink=sink, on_event=on_event,
            completion_context={"semantic_review_basis": basis},
            on_boundary=durable_generation_boundary_callback(durable=history.durable,
                session_id=history.session_id, task_id=history.run_id, sink=sink))))
    if outcome.stop_reason == "stopped":
        raise TaskCancelled("research assessment stopped")
    has_receipt = any(row.call.name == "submit_research_review" and row.ok and row.canonical.get("review_complete") is True
                      for row in session._memory_results.values())
    receipt = submitted_review(session, basis) if has_receipt else None
    if receipt is None and outcome.stop_reason in {"recovery_failure", "controller_failure", "delivery_pending", "provider_failure"}:
        raise ResearchContinuationError(outcome.summary)
    assessment = receipt["assessment"] if receipt is not None else None
    issues = review_issues(assessment) if assessment is not None else [{"kind": "review_incomplete",
        "reason": outcome.stop_reason}]
    # The operation's original result artifact owns this assessment, including
    # its provider response and exact frozen input. No separate review cursor.
    current = history.state()
    index = history.index(current)
    reference = current.work_ref if current.writer_attempt == invocation.attempt else index["results"][str(invocation.attempt)]
    return {**({key: receipt[key] for key in ("synthesis_id", "assessment_effect_id", "assessment_effect_ids")}
               if receipt is not None else {}),
        "schema": 3, "kind": "same_provider_model_assessment", "reviewer": provider_id,
        "basis_digest": basis["basis_digest"], "report_digest": basis["report_digest"],
        "result_ref": reference, "completion_status": "complete" if outcome.completed else "incomplete",
        "completion_stop_reason": outcome.stop_reason,
        "status": "incomplete" if assessment is None else "failed" if issues else "passed",
        "authority": "diagnostic_feedback_only",
        "assessment": assessment, "issues": issues}


def _submission_executor(history: Any, session: Any, basis: dict[str, Any], *, provider_id: str, cycle: int) -> Any:
    def submit(call: ToolCall) -> ToolResult:
        from codey.operations.task_session import turn_effect_id

        if call.args.get("basis_digest", basis["basis_digest"]) != basis["basis_digest"]:
            raise ValueError("assessment basis digest differs from the frozen report")
        current = next_batch(session, basis)
        if current is None:
            raise ValueError("all assessment batches are already submitted")
        assessment = parse_review(canonical_json(call.args["assessment"]), current)
        previous = batch_receipts(session, basis)
        complete = len(previous) + 1 == len(batch_scopes(basis))
        combined = merge_assessments([*[row["assessment"] for row in previous], assessment])
        if complete:
            combined = parse_review(canonical_json(combined), basis)
        issues = review_issues(combined)
        effect_id = turn_effect_id(f"{history.run_id}:research:review:{cycle}", session.turn, 0)
        metadata = {"schema": 3, "kind": "same_provider_model_assessment", "reviewer": provider_id,
            "basis_digest": basis["basis_digest"], "report_digest": basis["report_digest"],
            "assessment": combined, "issues": issues, "status": "failed" if issues else "passed",
            "authority": "diagnostic_feedback_only",
            "assessment_effect_id": effect_id,
            "assessment_effect_ids": [*[row["assessment_effect_id"] for row in previous], effect_id]}
        return ToolResult(call, model_text=canonical_json({"status": metadata["status"] if complete else "batch_recorded",
            "batch": current["batch_index"] + 1, "of": current["batch_count"],
            "next": "Assessment complete." if complete else "Submit only the next frozen batch in current observations."}), ok=True,
            canonical={**{key: value for key, value in metadata.items() if key != "assessment_effect_ids"},
                "assessment": assessment, "batch_digest": current["batch_digest"],
                "review_complete": complete, "synthesis_id": ""})
    return submit
