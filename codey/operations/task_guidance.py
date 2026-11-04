"""Select domain guidance at task composition, before entering the common loop."""

from typing import TYPE_CHECKING, Any

from codey.policies.output_requirement import guidance
from codey.policies.task_policy import TaskPolicy
from codey.research.completion_guidance import render_completion_guidance

if TYPE_CHECKING:
    from codey.operations.task_session import TaskSession
    from codey.research.ledger import ResearchLedger


def task_working_context(session: "TaskSession", research_ledger: "ResearchLedger | None" = None,
                         knowledge_store: Any = None, knowledge_session_id: str = "", *, research_scope: Any = None,
                         tool_names: tuple[str, ...] | None = None, provider: Any = None) -> str:
    from codey.operations.kernel_prompt import working_context
    from codey.research.completion_guidance import render_research_observations

    # Pure research has no workspace mutations to verify. Its task already
    # appears verbatim in the admitted prompt; repeating it here also repeats
    # parent reports/plans in investigations and obscures the latest evidence.
    research_only = (session.task_kind in {"research", "evidence_followup"}
                     and not session.policy.allows("project.write"))
    parts = [] if research_only else [working_context(session)]
    from codey.research.local_scope import current_scope

    scope = research_scope or (current_scope(session) if session.policy.strict_research else None)
    from codey.research.answer_contract import AnswerScope

    if isinstance(scope, AnswerScope):
        from codey.research.answer_context import answer_context, work_view_limits

        count, cap = work_view_limits(provider)
        return answer_context(session, research_ledger, scope, count_tokens=count, token_budget=cap)
    if scope is not None:
        from codey.research.context_view import local_research_context

        return local_research_context(session, research_ledger, knowledge_store, scope, tool_names=tool_names)
    if session.policy.strict_research and session.task_kind == "research" and not session.research_questions:
        from codey.knowledge.model import canonical_json
        from codey.research.plan_contract import task_layout

        return "Original complete task questions and binding context:\n" + canonical_json(task_layout(session.task_text))
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


def task_result_context(session: Any, result: Any, research_scope: Any) -> str:
    """Reuse complete reads and errors already present in the frozen work view."""
    from codey.operations.kernel_prompt import _result_context
    from codey.providers.base import render_result_metadata
    from codey.research.answer_contract import AnswerScope

    if (isinstance(research_scope, AnswerScope) and not result.ok and len(result.model_text) <= 1600
            and next(reversed(session._memory_results.values()), None) is result):
        identity = next((key for key, value in session._memory_results.items() if value is result), "")
        if identity:
            return ("Tool failed. The complete error appears in latest_error in the current evidence view.\n"
                    + render_result_metadata(identity, tool=result.call.name, ok=False))
    rows = result.canonical.get("read_passages", [])
    if isinstance(research_scope, AnswerScope) and result.ok and rows:
        labels = {ref: label for label, ref in research_scope.aliases}
        if all(row["ref"] in labels for row in rows):
            identity = next((key for key, value in session._memory_results.items() if value is result), "")
            if identity:
                return ("Read succeeded. The original window is pinned for citation as "
                        + ", ".join(f"[{labels[row['ref']]}]" for row in rows) + ".\n"
                        + render_result_metadata(identity, tool=result.call.name, ok=True))
    return _result_context(result, session, receipt_recovery=not isinstance(research_scope, AnswerScope))
