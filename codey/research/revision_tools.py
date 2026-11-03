"""Reveal the full revision tool from a settled, non-writing preparation receipt."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from codey.runtime.core.models import ToolCall, ToolResult

if TYPE_CHECKING:
    from codey.toolchain.tool_spec import ToolSpec

PREPARE = "prepare_knowledge_revision"
REVISION_GUIDANCE = (
    "Use knowledge_commit to edit existing stored objects with exact database references; "
    "current eN labels belong in the submission batch, not knowledge_commit. "
)
PREPARE_GUIDANCE = (
    "For authorized changes to stored objects, first call prepare_knowledge_revision to reveal knowledge_commit; "
    "preparation writes nothing. Current eN labels belong in the submission batch, not knowledge_commit. "
)
PREPARE_DESCRIPTION = (
    "Make the full knowledge_commit tool available next turn for authorized stored-knowledge changes. "
    "This only prepares the tool; it does not create, edit or verify knowledge. "
    "Ordinary lookup needs no preparation; new answer knowledge can use submit_research_answer.knowledge_updates."
)


def _denied(session: Any) -> set[str]:
    return {str(name or "").strip().lower() for name in (getattr(session, "controller_denied", ()) or ())}


def revision_ready(session: Any) -> bool:
    # Preserve the original recovery context after a failed read. Only settled
    # receipts count, so restart follows the same route without a mutable flag.
    return any((not result.ok and result.call.name == "knowledge_read")
               or (result.ok and result.call.name == PREPARE
                   and result.canonical.get("knowledge_revision_ready") is True)
               for result in getattr(session, "_memory_results", {}).values())


def preparation_spec() -> ToolSpec:
    from codey.toolchain.tool_spec import ToolSpec

    return ToolSpec(
        name=PREPARE, grant="knowledge.write", executor="knowledge", replay_class="safe",
        description=PREPARE_DESCRIPTION,
        json_examples=('{"tool":"prepare_knowledge_revision","args":{}}',),
    )


def revision_tool_names(session: Any, names: tuple[str, ...] | None) -> tuple[str, ...] | None:
    from codey.research.answer_contract import whole_research

    if (not whole_research(session) or names is None or "knowledge_commit" not in names
            or getattr(session, "model_tool_names", None) is not None or revision_ready(session)
            or PREPARE in _denied(session)):
        return names
    return tuple(PREPARE if name == "knowledge_commit" else name for name in names)


def revision_guidance(session: Any, guidance: str) -> str:
    from codey.research.answer_contract import whole_research

    if (whole_research(session) and getattr(session, "model_tool_names", None) is None
            and not revision_ready(session) and session.policy.allows("knowledge.write")
            and not {PREPARE, "knowledge_commit"}.intersection(_denied(session))):
        return guidance.replace(REVISION_GUIDANCE, PREPARE_GUIDANCE)
    return guidance


def preparation_result(session: Any, scope: object, call: ToolCall) -> ToolResult:
    from codey.research.answer_contract import AnswerScope, digest, whole_research

    if (not whole_research(session) or not isinstance(scope, AnswerScope)
            or scope.task_digest != digest(session.task_text) or call.args
            or not session.policy.allows("knowledge.write")
            or {PREPARE, "knowledge_commit"}.intersection(_denied(session))):
        return ToolResult(call, "ERROR: knowledge revision preparation is not authorized for this task.", ok=False)
    return ToolResult(call, "Revision tool prepared; knowledge_commit is available next turn. "
                      "No knowledge was created or changed. Read the target before revising it; "
                      "keep exact ids, expected revisions, scope and references.", ok=True,
                      canonical={"knowledge_revision_ready": True})
