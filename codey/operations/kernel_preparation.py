"""Prepare an unsent kernel turn once; received turns reuse their original input."""
from dataclasses import dataclass

from codey.codemode.json_edit import JSON_FIELD_EDIT_GUIDANCE
from codey.operations import kernel_prompt, kernel_protocol
from codey.operations.browser_text_report import report_scope
from codey.operations.browser_upload import upload_scope
from codey.operations.completion_context import CompletionContextInput
from codey.operations.kernel_protocol import TurnSnapshot
from codey.operations.project_prompt_context import prepare_coding_context
from codey.operations.task_session import TaskSession
from codey.policies.browser_requirement import parse_browser_requirement
from codey.policies.output_requirement import has_explanation, is_output_check
from codey.policies.tool_requirement import parse_tool_success_check


@dataclass(frozen=True)
class PreparedKernelTurn:
    snapshot: TurnSnapshot
    prompt: str


def prepare_kernel_turn(session: TaskSession, *, user_task: str, context_text: str,
                        task_guidance: str, native: bool, completion_context: CompletionContextInput | None = None,
                        request_scoped_contract: bool = False) -> PreparedKernelTurn:
    snapshot = kernel_protocol.build_turn_snapshot(session, native=native)
    from codey.research.answer_contract import AnswerScope

    if snapshot.policy is None:
        raise ValueError("prepared turn has no authorization policy")
    whole = isinstance(snapshot.research_scope, AnswerScope)
    guidance = [task_guidance] if task_guidance else []
    if snapshot.browser_copy is None and ".json" in user_task.casefold() and {"edit", "read_file"} <= set(snapshot.nested_tool_names):
        guidance.append(JSON_FIELD_EDIT_GUIDANCE)
    if (metadata := report_scope(session)) is not None:
        guidance.append(f"After browser_read returns the requested page facts, reuse them. On the same page, collect any missing "
                        f"metadata in a short separate CodeMode call: browser_eval with code {metadata.expression!r} and "
                        "browser_tab_list with empty arguments. Then use done.summary for the other requested facts and explanation. "
                        "The runtime fills the current tab URL and script token from the metadata receipts.")
    if (upload := upload_scope(session)) is not None and "browser_upload" in snapshot.inventory_names:
        plain_report = upload.report_hash and not any(is_output_check(check) for check in snapshot.policy.required_checks)
        guidance.append("If the initial runtime results show the authorized upload page is already open and required navigation "
                        "has succeeded, use that page for a fresh snapshot. Select the attachment once, then verify the page response."
                        + (" The runtime reports the exact file facts from the selection receipt. "
                           "In done.summary, report completion without copying the filename or SHA256." if plain_report else ""))
    if snapshot.policy.strict_research and snapshot.research_scope is None:
        from codey.research.methods import research_method_guidance

        guidance.append(research_method_guidance(user_task, evidence_only=session.task_kind == "evidence_followup"))
    for check_id in snapshot.policy.required_checks:
        requirement = parse_tool_success_check(check_id)
        if requirement is not None:
            guidance.append(requirement.model_guidance(explanation=has_explanation(snapshot.policy.required_checks)))
        elif (browser := parse_browser_requirement(check_id)) is not None:
            guidance.append(browser.guidance)
    prompt = kernel_prompt.kernel_prompt_for_session(
        session, user_task=user_task, contract_text=snapshot.contract_text,
        context_text=context_text,
        native=native, tool_names=snapshot.tool_names, task_guidance="\n\n".join(guidance),
        coding_context=prepare_coding_context(session, completion_context=completion_context),
        request_scoped_contract=request_scoped_contract,
        done_spec=next((spec for spec in snapshot.model_specs if spec.name == "done"), None),
        whole_submission=whole,
    )
    return PreparedKernelTurn(snapshot, prompt)
