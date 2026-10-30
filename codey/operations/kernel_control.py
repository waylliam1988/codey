"""Completion, questions and bounded protocol closure as explicit actions."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from codey.operations import kernel_prompt
from codey.operations.kernel_contracts import KernelResult
from codey.providers.base import AssistantTurn, ProviderToolResult
from codey.providers.response_record import json_value
from codey.runtime.core import operation_state as positions
from codey.runtime.core.operation_payload import RuntimeExecutionCounters, RuntimeOperationTransitionError


def result_from_payload(payload: Any) -> KernelResult:
    from codey.completion.contract import _proof_from_payload

    if (not isinstance(payload, dict) or set(payload) != {"completed", "summary", "turns", "stop_reason", "proof", "delivery"}
            or type(payload["completed"]) is not bool or not isinstance(payload["summary"], str)
            or type(payload["turns"]) is not int or payload["turns"] < 0
            or not isinstance(payload["stop_reason"], str)
            or payload["delivery"] not in {"not_required", "success", "failed", "unknown"}):
        raise ValueError("invalid persisted kernel result")
    proof = _proof_from_payload(payload["proof"]) if isinstance(payload["proof"], dict) else None
    if payload["proof"] is not None and proof is None:
        raise ValueError("invalid persisted completion proof")
    return KernelResult(payload["completed"], payload["summary"], payload["turns"], payload["stop_reason"],
                        proof=proof, delivery=payload["delivery"])


def validate_decision(payload: Any) -> dict[str, Any]:
    """Validate committed control facts before selecting any external action."""
    if not isinstance(payload, dict):
        raise ValueError("invalid control decision")
    purpose = payload.get("purpose")
    if purpose == "retry":
        required = {"purpose", "prompt", "error", "counters"}
        if (set(payload) not in (required, required | {"error_source", "error_type"})
                or not all(isinstance(payload[key], str) for key in ("prompt", "error"))):
            raise ValueError("invalid retry decision")
        try:
            RuntimeExecutionCounters.from_payload(payload["counters"])
        except RuntimeOperationTransitionError as exc:
            raise ValueError(str(exc)) from exc
        if "error_source" in payload and (not isinstance(payload["error_source"], str)
                or not isinstance(payload["error_type"], str)):
            raise ValueError("invalid completion diagnostic")
    elif purpose == "done":
        if (set(payload) != {"purpose", "result", "workspace_revision", "workspace_fingerprint"}
                or type(payload["workspace_revision"]) is not int or payload["workspace_revision"] < 0
                or not isinstance(payload["workspace_fingerprint"], str)):
            raise ValueError("invalid completion decision")
        result = result_from_payload(payload["result"])
        if not result.completed or result.stop_reason != "done":
            raise ValueError("completion decision has no accepted result")
    elif purpose == "question":
        from codey.task.questions import question_args

        if (set(payload) != {"purpose", "question", "question_id", "result"}
                or not isinstance(payload["question_id"], str) or not payload["question_id"]
                or not isinstance(payload["question"], dict)
                or question_args(payload["question"]) != payload["question"]):
            raise ValueError("invalid question decision")
        result = result_from_payload(payload["result"])
        if result.completed or result.stop_reason != "question":
            raise ValueError("question decision has no waiting result")
    elif purpose == "closing":
        if set(payload) != {"purpose", "result"}:
            raise ValueError("invalid closing decision")
        result_from_payload(payload["result"])
    else:
        raise ValueError("unknown control decision purpose")
    return payload


def evaluate_completion(context: Any, state: Any) -> None:
    from codey.operations.completion_gate import evaluate
    from codey.operations.project_verification import refresh_verification_candidates

    context.restore_facts()
    plan = context.plan(state)
    context.session.last_done_text = plan.control.body or ""
    context.session.last_done_args = dict(plan.control_args)
    error_source = ""
    error_type = ""
    try:
        completion_context = context.completion_context
        if context.session.policy.strict_research and context.session.task_kind != "evidence_followup":
            from codey.research.finding_report import compile_finding_report

            tools = context.execution.research_tools
            try:
                compiled = compile_finding_report(context.session.last_done_args, tools.ledger, tools.store,
                    project=tools.project, session_id=tools.session_id)
            except ValueError as exc:
                # Keep the repair field-local and the original proposal intact.
                raise ValueError(f"Report reference fields: {exc}") from exc
            context.session.last_done_text = compiled.text
            if compiled.validation_ledger is not None:
                completion_context = {**context.completion_context, "research_ledger": compiled.validation_ledger,
                                      "research_execution_ledger": tools.ledger,
                                      "research_inference_statements": compiled.inference_statements}
        refresh_verification_candidates(context.session)
        verdict = evaluate(context.session, context.session.last_done_text, context=completion_context)
        if type(verdict.complete) is not bool:
            raise ValueError("completion gate returned an invalid verdict")
        complete, followup, proof = verdict.complete, verdict.followup, verdict.proof
        error_source = getattr(verdict, "error_source", "")
        error_type = getattr(verdict, "error_type", "")
        final_text = verdict.final_text or context.session.last_done_text
    except Exception as exc:
        complete, proof, final_text = False, None, context.session.last_done_text
        followup = f"Completion check failed ({exc}); cannot complete yet. Continue the task."
        error_source = "completion_check"
        error_type = type(exc).__name__
    if complete:
        result = KernelResult(True, final_text, context.session.turn, "done", proof=proof)
        context.advance(state, positions.LEAF_CONTROL_READY, data={"purpose": "done", "result": json_value(result),
            "workspace_revision": context.session.workspace_revision, "workspace_fingerprint": context.session.workspace_fingerprint})
    else:
        from codey.operations.kernel_progress import KernelProgress

        progress = KernelProgress(context.transport.stagnant_turns, snapshot=state.counters.progress)
        progress.reject_completion(context.session.last_done_args, followup, context.session)
        counters = replace(state.counters, progress=progress.snapshot())
        context.advance(state, positions.LEAF_CONTROL_READY, data={"purpose": "retry",
            "prompt": kernel_prompt._completion_followup(followup, native=context.native,
                structured_report="report_sections" in context.session.last_done_args), "error": "",
            "counters": counters.to_payload(), "error_source": error_source, "error_type": error_type},
            counters=counters)


def _ids(reply: Any) -> list[str]:
    return list(dict.fromkeys(call.id for call in reply.tool_calls if call.id)) if isinstance(reply, AssistantTurn) else []


def no_progress_result(session: Any, turn: int) -> KernelResult:
    from codey.operations.explicit_execution_requirements import readonly_fix_obstruction

    obstruction = readonly_fix_obstruction(session)
    return KernelResult(False, obstruction or "stopped after repeated tool results without progress", turn,
                        "blocked" if obstruction is not None else "no_progress")


def prepare_control_delivery(context: Any, state: Any) -> None:
    decision = context.decision(state)
    received = context.response(state)
    ids = _ids(received.reply)
    purpose = decision["purpose"]
    if purpose == "retry":
        turn = received.logical_turn
        from codey.operations.kernel_progress import KernelProgress

        if KernelProgress(context.transport.stagnant_turns, snapshot=state.counters.progress).stopped:
            context.close(state, KernelResult(False,
                "Stopped after repeated rejected completion proposals without progress.\n\n"
                + decision["prompt"] + "\n\nUnaccepted draft:\n" + context.session.last_done_text,
                turn, "no_progress"))
            return
        if turn >= context.session.max_turns and not (context.native and ids):
            context.close(state, KernelResult(False, context.session.last_done_text or "max turns reached", turn, "max_turns"))
            return
        from codey.operations.task_guidance import task_working_context

        # Historical observations precede the specific current rejection so
        # they cannot bury the next action in several pages of saved findings.
        observations = "" if context.request_scoped_observations else task_working_context(
            context.session, getattr(context.execution.research_tools, "ledger", None),
                getattr(context.execution.research_tools, "store", None))
        prompt = "\n\n".join(part for part in (observations, decision["prompt"]) if part)
        messages = None
        if context.native and ids:
            messages = [ProviderToolResult(identity, "ERROR: " + (decision["error"] or prompt)) for identity in ids]
            if decision["error"]:
                messages[-1] = replace(messages[-1], user_content=prompt)
        snapshot = received.snapshot
        # Native rejection replies use the exact declarations that answered
        # the original calls. A text retry starts the next ordinary turn.
        if messages is None:
            from codey.operations.kernel_protocol import build_turn_snapshot
            snapshot = build_turn_snapshot(context.session, native=context.native)
        context.freeze(state, snapshot, turn=turn + 1, prompt=prompt, messages=messages,
                       counters=RuntimeExecutionCounters.from_payload(decision["counters"]))
        return
    if purpose == "question":
        from codey.durable.questions import DurableUserQuestionStore
        from codey.runtime.observe.events import RunEvent
        if context.observation.on_event is None:
            raise ValueError("user questions require an event surface")
        task = context.durable.task(context.sink.run_id)
        DurableUserQuestionStore(context.durable).request(decision["question_id"], context.sink.session_id,
            context.sink.run_id, decision["question"], {"request_ref": state.response_ref,
                "input_request": dict(task.state.get("input_request", {}))})
        # Notification is a projection of the saved question, so its failure
        # leaves the same stable question available after a restart.
        context.observation.on_event(RunEvent("user_question", turn=received.logical_turn,
            message=decision["question"]["question"], metadata={"id": decision["question_id"], **decision["question"]}))
        text = "Question recorded; waiting for the user. No execution authorized."
    elif purpose == "done":
        store = context.execution.workspace_revision_store
        project = context.execution.project_path
        if store is not None and project is not None:
            current = store.current_state(str(project), ignored_paths=context.execution.workspace_ignored_paths)
            if (current.revision != decision["workspace_revision"]
                    or current.fingerprint != decision["workspace_fingerprint"]):
                context.session.set_workspace_state(current.revision, current.fingerprint)
                context.advance(state, positions.LEAF_COMPLETION_PENDING)
                return
        text = "OK: done accepted: " + decision["result"]["summary"][:500]
    else:
        raise ValueError("unknown control delivery purpose")
    if context.native and ids:
        context.freeze(state, received.snapshot, turn=received.logical_turn,
            messages=[ProviderToolResult(identity, text) for identity in ids], acknowledge=True)
    else:
        context.advance(state, positions.LEAF_CLOSING)


def close_protocol(context: Any, view: Any) -> None:
    state = view.state
    decision = context.decision(state)
    result = result_from_payload(decision["result"])
    received = context.response(state) if state.response_ref else None
    messages: list[ProviderToolResult] = []
    if received is not None:
        # A completed tool batch owns its original success/failure receipts;
        # closure must deliver those before rejecting any new calls.
        batch = next((batch for batch in view.batches if not batch.is_terminal
                      and batch.intent.turn == state.turn and not batch.send_attempts), None)
        if batch is not None:
            from codey.operations.kernel_transport import _native_tool_results
            context.sink.delivery_batch_id = batch.intent.batch_id
            if context.native:
                messages = _native_tool_results(context.results(view, batch.intent.batch_id), context.session)
        if context.native and not messages:
            messages = [ProviderToolResult(identity, "ERROR: " + result.summary + "; tool call was not executed")
                        for identity in _ids(received.reply)]
    if messages:
        assert received is not None
        if state.counters.closing_round >= 4:
            result = replace(result, completed=False, stop_reason="protocol", delivery="failed",
                summary=result.summary + "; protocol close failed after four acknowledgements")
            context.close(state, result)
            state = context.load_view().state
        else:
            context.freeze(state, received.snapshot, turn=received.logical_turn, messages=messages,
                acknowledge=True, counters=replace(state.counters, closing_round=state.counters.closing_round + 1))
            return
    # A text batch still requires an explicit abandonment, never a fabricated
    # provider delivery. The existing lifecycle settlement validates receipts.
    from codey.operations.generation_boundary import GenerationBoundaryRequest
    if context.observation.on_boundary is None:
        raise ValueError("kernel settlement requires the durable generation boundary")
    boundary = "final" if result.stop_reason == "done" and result.turns < context.session.max_turns else "settle"
    context.observation.on_boundary(GenerationBoundaryRequest(boundary, settle_kernel=True, expected=state))
