"""Bounded domain assessment/repair through the single durable operation driver."""

from __future__ import annotations

from dataclasses import replace
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
    reviewed_completion,
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
                          max_turns=3 * len(batch_scopes(basis)), task_text=prompt, coding_context_enabled=False,
                          research_review_basis=basis)
    executor = _submission_executor(history, session, basis, result, tools, provider_id=provider_id,
                                    cycle=cycle, policy=parent_policy, on_event=on_event)
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
    if receipt is not None and receipt.get("synthesis_id") and receipt["synthesis_id"] not in tools.created_ids:
        tools.created_ids.append(receipt["synthesis_id"])
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
        "schema": 1, "kind": "same_provider_model_assessment", "reviewer": provider_id,
        "basis_digest": basis["basis_digest"], "report_digest": basis["report_digest"],
        "result_ref": reference, "completion_status": "complete" if outcome.completed else "incomplete",
        "completion_stop_reason": outcome.stop_reason,
        "status": "incomplete" if assessment is None else "failed" if issues else "passed",
        "assessment": assessment, "issues": issues}


def _submission_executor(history: Any, session: Any, basis: dict[str, Any], result: Any, tools: Any, *,
                         provider_id: str, cycle: int, policy: Any, on_event: Any) -> Any:
    def submit(call: ToolCall) -> ToolResult:
        from codey.operations.research_iteration import _persist_synthesis
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
        metadata = {"schema": 1, "kind": "same_provider_model_assessment", "reviewer": provider_id,
            "basis_digest": basis["basis_digest"], "report_digest": basis["report_digest"],
            "assessment": combined, "issues": issues, "status": "failed" if issues else "passed",
            "assessment_effect_id": effect_id,
            "assessment_effect_ids": [*[row["assessment_effect_id"] for row in previous], effect_id]}
        identity = ""
        if complete and not issues:
            # Validate the combined acceptance contract before publishing. The
            # real result artifact replaces this effect ref once it is settled.
            reviewed_completion(result.completion_proof, {**metadata, "result_ref": effect_id})
            identity = _persist_synthesis(tools, result.question, result.summary, session_id=history.session_id,
                project=tools.project, run_id=history.run_id, policy=policy, accepted_report=result.accepted_report,
                projection="reviewed", on_event=on_event, semantic_review=metadata)
            if not identity:
                raise ResearchContinuationError("accepted research report was not published")
        return ToolResult(call, model_text=canonical_json({"status": metadata["status"] if complete else "batch_recorded",
            "batch": current["batch_index"] + 1, "of": current["batch_count"],
            "next": "Assessment complete." if complete else "Submit only the next frozen batch in current observations."}), ok=True,
            canonical={**{key: value for key, value in metadata.items() if key != "assessment_effect_ids"},
                "assessment": assessment, "batch_digest": current["batch_digest"],
                "review_complete": complete, "synthesis_id": identity})
    return submit


def review_and_repair(result: Any, tools: Any, *, history: Any, provider: Any, provider_id: str,
                      on_event: Any, stop_flag: Any, repair: Any, parent_policy: Any) -> Any:
    """Strategy chooses successors; shared driver owns every actual request."""
    if result.stop_reason != "done" or result.accepted_report is None:
        return result
    for cycle in range(3):
        assessment = assess_report(history, result, tools, provider=provider, provider_id=provider_id,
                                   cycle=cycle, on_event=on_event, stop_flag=stop_flag, parent_policy=parent_policy)
        current = history.state()
        used = current.turns_used + current.logical_turn
        result = replace(result, semantic_review=assessment, turns=used)
        if assessment["completion_status"] != "complete" or assessment["status"] == "incomplete":
            return replace(result, stop_reason="research_review_incomplete", completion_proof=None,
                synthesis_id=assessment.get("synthesis_id", ""),
                notes_created=list(tools.created_ids),
                quality_warnings=[*result.quality_warnings,
                    "Assessment completion is incomplete; its settled judgment and publication are retained."])
        if assessment["status"] == "passed":
            return replace(result, completion_proof=reviewed_completion(result.completion_proof, assessment))
        if cycle == 2 or used >= current.turn_budget:
            return replace(result, stop_reason="semantic_review_failed", completion_proof=None,
                quality_warnings=[*result.quality_warnings,
                    "Evidence-bound model assessment failed; this report is an unaccepted draft."])
        feedback = ("Repair the draft against the original task and exact selected evidence. "
            "The assessment is fallible feedback; verify each issue against original excerpts. "
            "Append any missing task requirements using research_plan, keeping existing questions in order. "
            "For an unsupported claim, save a narrower corrected finding, select additional read passages, "
            "or investigate missing evidence. Cite corrected findings; do not change a candidate's review status. "
            "For report overreach, narrow the specific sentence. Preserve real evidence gaps explicitly.\n"
            "Assessment issues (data):\n" + canonical_json(assessment["issues"]))
        result = repair(ResearchInvocation.repair(cycle), feedback)
        if result.stop_reason != "done" or result.accepted_report is None:
            return replace(result, semantic_review=assessment, completion_proof=None)
    raise AssertionError("bounded assessment did not terminate")
