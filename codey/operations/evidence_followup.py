"""Run bounded Research evidence extraction through the shared task kernel."""

from __future__ import annotations

from typing import Any

from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
)
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.evidence_rules import (
    EvidenceFollowupController,
    EvidenceFollowupResult,
    build_evidence_followup_prompt,
    build_evidence_followup_repair_prompt,
)
from codey.runtime.core.models import ToolCall, ToolResult
from codey.utils.refs import clip


def _subset_followup_policy(parent: Any) -> TaskPolicy:
    """Follow-up 权限恒为父任务子集；无父策略时仅保留 control（绝不自授）。"""
    if parent is None:
        return TaskPolicy(grants=frozenset({"control"}))
    return TaskPolicy(grants=frozenset(
        grant for grant in ("control", "knowledge.write") if parent.allows(grant)
    ))


def run_evidence_followup(
    *,
    provider: Any,
    tools: Any,
    plan: Any,
    material: Any,
    question: str,
    parent_policy: Any,
    initial_summary: str = "",
    max_context_chars: int = 8000,
    should_stop: Any = None,
    session_id: str = "",
    run_id: str = "",
    provider_id: str = "",
    round_index: int = 1,
    runtime_mutations: Any = None,
    durable: Any = None,
    managed_outputs: Any = None,
    on_event: Any = None,
) -> EvidenceFollowupResult:
    fresh_urls = tuple(getattr(material, "fresh_source_urls", ()) or ())
    if should_stop is not None and should_stop():
        return EvidenceFollowupResult(stop_reason="stopped")
    if not fresh_urls:
        return EvidenceFollowupResult(ok=True, stop_reason="no_fresh_urls")

    prior_evidence = len(getattr(tools.ledger, "evidence_items", ()) or ())
    prior_notes = set(getattr(tools, "created_ids", ()) or ())
    prompt = build_evidence_followup_prompt(
        question=question, initial_summary=initial_summary,
        plan=plan, material=material, max_context_chars=max_context_chars,
    )
    errors: list[str] = []
    last_summary = ""
    for attempt in (1, 2):
        if should_stop is not None and should_stop():
            return EvidenceFollowupResult(stop_reason="stopped")
        last_error: str = ""

        def write(call: ToolCall) -> ToolResult:
            nonlocal last_error
            from codey.research.tools import clone_research_tools

            staged = clone_research_tools(tools)
            staged.ledger = tools.ledger.clone()
            prior_created, prior_updated = set(tools.created_ids), set(tools.updated_ids)
            controller = EvidenceFollowupController(staged, fresh_urls)
            before = len(tools.ledger.evidence_items)
            result = controller.execute_tool_call(call.name, dict(call.args or {}))
            if not result.ok:
                last_error = clip(result.model_text, 200)
            from codey.research.ledger_receipts import ledger_observation

            canonical = {"research_observation": ledger_observation(staged.ledger, call.name, evidence_start=before)} if result.ok else {}
            if result.ok:
                from codey.providers.response_record import json_value
                from codey.research.tools import StagedKnowledgeStore

                created = [identity for identity in tools.created_ids if identity not in prior_created]
                proposals: list[object] = []
                if isinstance(tools.store, StagedKnowledgeStore):
                    proposals = [json_value(tools.store._staged_notes[identity]) for identity in created]
                canonical["knowledge_notes"] = {
                    "created": created,
                    "updated": [identity for identity in tools.updated_ids if identity not in prior_updated],
                    "proposals": proposals,
                }
            return ToolResult(call=call, model_text=result.model_text, ok=result.ok, canonical=canonical)

        session = TaskSession(
            policy=_subset_followup_policy(parent_policy),
            task_kind="evidence_followup", max_turns=1, task_text=prompt,
        )
        active_provider = provider
        sink = None
        if runtime_mutations is not None and session_id and run_id:
            from codey.operations.task_effects import KernelEffectSink

            sink = KernelEffectSink(
                runtime_mutations, session_id=session_id, run_id=run_id,
                provider_id=provider_id, phase="research",
                durable=durable,
                managed_outputs=managed_outputs,
            )
        from codey.operations.research_continuation import ResearchContinuation

        history = ResearchContinuation(runtime_mutations, managed_outputs, session_id=session_id, run_id=run_id)
        outcome = history.kernel(
            session,
            attempt=2 + (round_index - 1) * 2 + attempt - 1,
            request=KernelRunRequest(
                transport=KernelTransportDeps(
                    provider=active_provider,
                    run_id=run_id,
                    effect_scope=f"research:followup:{round_index}:attempt:{attempt}",
                    provider_id=provider_id,
                    user_task=prompt,
                ),
                execution=KernelExecutionDeps(executors={"knowledge_write": write}, research_tools=tools),
                observation=KernelObservationDeps(
                    intent_sink=sink,
                    on_boundary=durable_generation_boundary_callback(
                        durable=durable,
                        session_id=session_id,
                        task_id=run_id,
                        sink=sink,
                    ),
                    on_event=on_event,
                    completion_context={
                        "run_id": run_id,
                        "task": question,
                        "question": question,
                    },
                ),
            ),
        )
        if outcome.stop_reason in {"recovery_failure", "controller_failure", "delivery_pending", "provider_failure"}:
            from codey.research.pipeline import ResearchContinuationError
            raise ResearchContinuationError(outcome.summary)
        if not last_error:
            failures = [item for item in session._memory_results.values() if not item.ok]
            if failures:
                last_error = clip(failures[-1].model_text, 200)
        last_summary = outcome.summary
        new_count = max(0, len(getattr(tools.ledger, "evidence_items", ()) or ()) - prior_evidence)
        note_ids = tuple(item for item in (getattr(tools, "created_ids", ()) or ())
                         if item not in prior_notes)
        if new_count or note_ids:
            return EvidenceFollowupResult(
                ok=True, written_note_ids=note_ids, new_evidence_count=new_count,
                new_source_urls=fresh_urls, stop_reason="written",
                errors=tuple(errors[:10]),
            )
        if outcome.stop_reason == "done":
            from codey.research.evidence_rules import _done_reports_no_relevant_material

            no_relevant = _done_reports_no_relevant_material({"summary": last_summary})
            return EvidenceFollowupResult(
                ok=False, new_source_urls=fresh_urls,
                stop_reason="no_relevant_material" if no_relevant else "no_evidence_extracted",
                errors=tuple(errors[:10]),
            )
        if last_error:
            errors.append(last_error)
        else:
            errors.append(f"Evidence-only follow-up did not save evidence ({outcome.stop_reason})")
        prompt = build_evidence_followup_repair_prompt(
            question=question, plan=plan, material=material,
            validation_error=errors[-1], max_context_chars=max_context_chars,
        )
    return EvidenceFollowupResult(
        ok=False, new_source_urls=fresh_urls, stop_reason="no_evidence_extracted",
        errors=tuple(errors[:10]) or (clip(last_summary, 200),),
    )


__all__ = ["run_evidence_followup"]
