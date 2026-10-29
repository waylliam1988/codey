"""Investigate a research plan using ordinary shared-kernel tool turns.

The strategy supplies a question and bounds. Sources, evidence and progress come
only from normal operation effects; there is no direct PlanExecutor acquisition.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from codey.knowledge.model import canonical_json
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
)
from codey.operations.research_continuation import ResearchContinuation
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.response_record import json_value
from codey.research.evidence_rules import EvidenceFollowupResult
from codey.research.methods import research_method_guidance
from codey.research.pipeline import ResearchContinuationError
from codey.research.plan_executor import PlanExecutionResult
from codey.research.query_planner import ResearchPlan
from codey.research.tools import ResearchTools
from codey.runtime.core import cancellation
from codey.runtime.effects.effect_records import compute_args_digest


def investigate_plan(*, history: ResearchContinuation, plan: ResearchPlan, tools: ResearchTools,
                     provider: Any, parent_policy: Any, question: str, initial_summary: str,
                     round_index: int, provider_id: str, should_stop: Callable[[], bool],
                     on_event: Any = None) -> tuple[PlanExecutionResult, EvidenceFollowupResult]:
    policy = TaskPolicy(grants=frozenset(grant for grant in (
        "control", "web.read", "knowledge.read", "knowledge.write"
    ) if parent_policy is not None and parent_policy.allows(grant)))
    if not policy.allows("web.read"):
        return PlanExecutionResult(stop_reason="policy_denied"), EvidenceFollowupResult(stop_reason="policy_denied")
    baseline_urls = set(tools.ledger.final_url_set())
    baseline_evidence = len(tools.ledger.evidence_items)
    baseline_searches = len(tools.ledger.searches)
    baseline_ids = set(tools.created_ids)
    plan_text = canonical_json(json_value(plan))
    history.investigation_plan(round_index, plan, tools)
    scope = f"research:investigation:{round_index}:" + compute_args_digest(plan_text)[:16]
    parent_findings = [{"claim": item.claim, "source_url": item.source_url, "stance": item.stance}
                       for item in tools.ledger.evidence_items[-12:]]
    prompt = ("Current task: collect missing evidence for the parent research; deliver a brief acquisition receipt.\n"
              f"Parent question (context, not this phase's deliverable):\n{question}\n"
              "Already saved parent findings (data, not work performed in this phase):\n"
              f"{canonical_json(parent_findings)}\n"
              "Previously opened sources (use their URLs with source_search to find missing passages):\n"
              f"{canonical_json(sorted(baseline_urls))}\n"
              f"Investigation plan (data):\n{plan_text}\n"
              "Use web_search and open_url/open_result to obtain missing direct evidence or counterevidence. "
              f"Use at most {plan.max_queries} queries and {plan.max_sources} fresh sources. "
              "Save directly relevant exact excerpts with knowledge_write (type=fact, sources and nonempty evidence). "
              "Preserve applicability conditions and contradictions; do not rewrite the final report here. "
              "First attempt a targeted investigation of a missing comparison or condition. "
              "Do not end immediately by repeating parent findings. Reuse opened sources where useful; "
              "the plan's queries are suggestions, not a substitute for identifying the actual gap. "
              "Use done to describe remaining gaps when no more useful material is available.\n"
              + research_method_guidance(question, evidence_only=True)
              + "\nCurrent deliverable: save new evidence through the tools, then call done with only a brief "
                "summary of what actually saved and the remaining gaps. The parent owns final report synthesis. "
                "Do not copy its report, headings, or report_sections into this phase's response.")
    stop_reason = "no_new_material"
    if should_stop():
        raise cancellation.TaskCancelled("research investigation stopped")
    attempt = 2 + (round_index - 1) * 2
    sink = KernelEffectSink(history.mutations, session_id=history.session_id, run_id=history.run_id,
                            provider_id=provider_id, phase="research", managed_outputs=history.outputs,
                            durable=history.durable)
    session = TaskSession(policy=policy, task_kind="evidence_followup", max_turns=max(3, min(12,
        plan.max_queries * 2 + plan.max_sources + 1)), task_text=prompt,
        controller_denied=("knowledge_commit", "knowledge_link"),
        research_acquisition_limits=(plan.max_queries, plan.max_sources))
    outcome = history.kernel(session, attempt=attempt, request=KernelRunRequest(
        task_guidance="Collect missing evidence through the tools, preserving unknown conditions.",
        transport=KernelTransportDeps(provider=provider, run_id=history.run_id,
            effect_scope=scope, provider_id=provider_id, user_task=prompt,
            stagnant_turns=5,
            stop_flag=_StopFlag(should_stop)),
        execution=KernelExecutionDeps(research_tools=tools, managed_outputs=history.outputs,
                                       session_id=history.session_id, durable=history.durable),
        observation=KernelObservationDeps(intent_sink=sink, on_event=on_event,
            on_boundary=durable_generation_boundary_callback(durable=history.durable,
                session_id=history.session_id, task_id=history.run_id, sink=sink),
            completion_context={"research_ledger": tools.ledger, "source_ids": session.source_ids})))
    if outcome.stop_reason == "stopped":
        # Durable cancellation may arrive without setting the outer process's
        # stop flag. Never turn it into "no new material" and return an older
        # accepted draft as a successful completed research task.
        raise cancellation.TaskCancelled("research investigation stopped")
    if outcome.stop_reason in {"recovery_failure", "controller_failure", "delivery_pending", "provider_failure"}:
        raise ResearchContinuationError(outcome.summary)
    stop_reason = outcome.stop_reason
    fresh_urls = tuple(sorted(set(tools.ledger.final_url_set()) - baseline_urls))
    material = PlanExecutionResult(
        queries_executed=tuple(row.query for row in tools.ledger.searches[baseline_searches:]),
        opened_sources=tuple(source.to_dict() for source in tools.ledger.opened_sources if source.final_url in fresh_urls),
        previews=tuple(tools.ledger.source_text_for_url(url)[:2400] for url in fresh_urls),
        fresh_source_urls=fresh_urls, fresh_source_count=len(fresh_urls),
        baseline_source_urls=tuple(sorted(baseline_urls)), stop_reason="opened_sources" if fresh_urls else "no_new_material")
    count = len(tools.ledger.evidence_items) - baseline_evidence
    followup = EvidenceFollowupResult(ok=count > 0, stop_reason=stop_reason, new_evidence_count=count,
        written_note_ids=tuple(identity for identity in tools.created_ids if identity not in baseline_ids),
        new_source_urls=fresh_urls)
    return material, followup


class _StopFlag:
    def __init__(self, callback: Callable[[], bool]) -> None:
        self.callback = callback

    def is_set(self) -> bool:
        return self.callback()
