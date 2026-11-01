"""Select domain guidance at task composition, before entering the common loop."""

from typing import TYPE_CHECKING, Any

from codey.policies.output_requirement import guidance
from codey.policies.task_policy import TaskPolicy
from codey.research.completion_guidance import render_completion_guidance

if TYPE_CHECKING:
    from codey.operations.task_session import TaskSession
    from codey.research.ledger import ResearchLedger


def task_working_context(session: "TaskSession", research_ledger: "ResearchLedger | None" = None,
                         knowledge_store: Any = None, knowledge_session_id: str = "", *, research_scope: Any = None) -> str:
    from codey.operations.kernel_prompt import working_context
    from codey.research.completion_guidance import render_research_observations

    if session.task_kind == "research_review" and session.research_review_basis is not None:
        from codey.research.review_batches import next_batch
        from codey.research.semantic_review import review_prompt

        current = next_batch(session, session.research_review_basis)
        return review_prompt(current) if current is not None else "All frozen assessment batches are recorded."

    # Pure research has no workspace mutations to verify. Its task already
    # appears verbatim in the admitted prompt; repeating it here also repeats
    # parent reports/plans in investigations and obscures the latest evidence.
    research_only = (session.task_kind in {"research", "evidence_followup", "research_review"}
                     and not session.policy.allows("project.write"))
    parts = [] if research_only else [working_context(session)]
    from codey.research.local_scope import current_scope

    scope = research_scope or (current_scope(session) if session.policy.strict_research else None)
    if scope is not None:
        from codey.knowledge.model import canonical_json

        parts.append("Current frozen local research target:\n" + canonical_json(scope.to_payload())
            + "\nAnswer only this target against its direct evidence. Save one complete supported relationship, "
            "then submit_research_answer with answered/partial and selected fN. If direct evidence is absent, "
            "submit unresolved with findings=[]; no placeholder finding is required. "
            "This submission ends the local target, not the parent report. Do not call global done.")
    if session.policy.strict_research or session.task_kind == "evidence_followup":
        parts.append(render_research_observations(session, research_ledger, knowledge_store, session_id=knowledge_session_id))
        if session.policy.strict_research and session.task_text.strip():
            from codey.knowledge.model import canonical_json
            from codey.research.task_requirements import task_requirement_context

            parts.append("Original task requirements (text accounting, not semantic verification):\n"
                         + canonical_json(task_requirement_context(session.task_text)))
    return "\n\n".join(parts)


def task_guidance_for_policy(policy: TaskPolicy) -> str:
    return "\n".join(part for part in (
        render_completion_guidance(policy) if policy.strict_research else "",
        ("Knowledge actions: when the user requests saving or revising knowledge, execute save_finding, knowledge_write or "
         "knowledge_commit and wait for success before calling done. Tool JSON inside a code fence or a report "
         "is an example, not a saved change. Keep candidate analogies distinct from verified findings."
         if policy.allows("knowledge.write") and not policy.strict_research else ""),
        ("Use knowledge_read on the relevant versions before revising or adopting them; "
         "copy their actual ids and revisions. If background recall lacks the requested topic or object kind, "
         "search for that target before editing. A related premise is not the inference being revised."
         if policy.allows("knowledge.read") and policy.allows("knowledge.write") and not policy.strict_research else ""),
        guidance(policy.required_checks),
    ) if part)
