"""Run bounded Research evidence extraction through the shared task kernel."""

from __future__ import annotations

from typing import Any

from codey.operations.task_loop import TaskSession, run_task_kernel
from codey.policies.task_policy import TaskPolicy
from codey.research.evidence_followup import (
    EvidenceFollowupController,
    EvidenceFollowupResult,
    build_evidence_followup_prompt,
    build_evidence_followup_repair_prompt,
)
from codey.runtime.core.models import ToolCall
from codey.utils.refs import clip


def run_evidence_followup(
    *,
    provider: Any,
    tools: Any,
    plan: Any,
    material: Any,
    question: str,
    initial_summary: str = "",
    max_context_chars: int = 8000,
    should_stop: Any = None,
    session_id: str = "",
    run_id: str = "",
    provider_id: str = "",
    round_index: int = 1,
    runtime_mutations: Any = None,
    on_event: Any = None,
) -> EvidenceFollowupResult:
    fresh_urls = tuple(getattr(material, "fresh_source_urls", ()) or ())
    if should_stop is not None and should_stop():
        return EvidenceFollowupResult(stop_reason="stopped")
    if not fresh_urls:
        return EvidenceFollowupResult(ok=True, stop_reason="no_fresh_urls")

    controller = EvidenceFollowupController(tools, fresh_urls)
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
        last_error = ""

        def write(call: ToolCall) -> str:
            nonlocal last_error
            result = controller.execute_tool_call(call.name, dict(call.args or {}))
            if str(result).startswith("ERROR:"):
                last_error = clip(str(result), 200)
            return str(result)

        session = TaskSession(
            policy=TaskPolicy(grants=frozenset({"control", "knowledge.write"})),
            task_kind="evidence_followup", max_turns=1, task_text=prompt,
        )
        active_provider = provider
        sink = None
        if runtime_mutations is not None and session_id and run_id:
            from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider

            runtime_mutations.mark_writer_running(session_id, run_id, provider_id=provider_id)
            sink = KernelEffectSink(
                runtime_mutations, session_id=session_id, run_id=run_id,
                provider_id=provider_id, phase="research",
            )
            active_provider = KernelRecordedProvider(provider, sink)
        outcome = run_task_kernel(
            session, provider=active_provider, run_id=run_id,
            effect_scope=f"research:followup:{round_index}:attempt:{attempt}",
            provider_id=provider_id, executors={"knowledge_write": write},
            user_task=prompt, intent_sink=sink, on_event=on_event,
        )
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
            from codey.research.evidence_followup import _done_reports_no_relevant_material

            no_relevant = _done_reports_no_relevant_material({"answer": last_summary})
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
