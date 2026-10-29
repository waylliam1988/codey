"""Resource-bound execution of the operation reducer's concrete actions.

The operation checkpoint owns position; effects own admission and receipts.
This object contains dependencies and rebuildable caches only.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

from codey.durable.checkpoint import checkpoint_entries
from codey.durable.model import thaw_json
from codey.operations import kernel_actions as wiring
from codey.operations import kernel_events, kernel_prompt, kernel_protocol, kernel_transport
from codey.operations.kernel_contracts import KernelResult, KernelRunRequest
from codey.operations.kernel_errors import RecoveryFailed
from codey.operations.kernel_preparation import prepare_kernel_turn
from codey.operations.kernel_progress import KernelProgress
from codey.operations.kernel_trace import record_outbound, record_turn
from codey.operations.task_guidance import task_working_context
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import ProviderToolResult, TurnFinish, tools_from_specs
from codey.providers.response_record import json_value
from codey.runtime.core import operation_state as positions
from codey.runtime.core.operation_payload import RuntimeExecutionCounters
from codey.runtime.effects.effect_records import RuntimeEffectStore
from codey.runtime.log.session_view import driver_for_state, load_session_view, not_sent_continuation_effect
from codey.workspace.context_admission import admit_ghost_context


class KernelContext:
    def __init__(self, session: Any, request: KernelRunRequest) -> None:
        self.session, self.request = session, request
        self.transport, self.execution, self.observation = request.transport, request.execution, request.observation
        self.sink = self.observation.intent_sink
        if self.sink is None or self.sink.durable is None or self.sink.managed_outputs is None:
            raise ValueError("kernel execution requires an admitted durable operation and managed output storage")
        self.durable = self.sink.durable
        admitted = self.load_view().state
        if admitted is None:
            raise ValueError("kernel execution requires an operation checkpoint")
        self.sink.phase = driver_for_state(admitted)
        session.max_turns = min(session.max_turns, max(0, admitted.turn_budget - admitted.turns_used))
        self.effect_scope = self.transport.effect_scope
        self.provider = self.transport.provider
        self.native = kernel_transport.provider_uses_native(self.provider, provider_id=self.transport.provider_id)
        self.window = getattr(self.provider, "window_ref", None) or self.transport.window_ref
        self.completion_context = wiring._completion_context_with_ignores(
            self.observation.completion_context, self.execution.workspace_ignored_paths)
        if self.execution.research_tools is not None and isinstance(self.completion_context, dict):
            tools = self.execution.research_tools
            self.completion_context.update(knowledge_store=getattr(tools, "store", None),
                                           knowledge_project=getattr(tools, "project", ""),
                                           knowledge_session_id=getattr(tools, "session_id", ""))
        self.responses: dict[str, Any] = {}
        self.projected_provider_effects: set[str] = set()
        self.snapshots: dict[int, Any] = {}
        self.attempt_gate_bound = wiring._bind_attempt_gate(self.provider, self.transport, self.observation.trace_recorder)

    @property
    def request_scoped_context(self) -> bool:
        return not self.native and getattr(self.provider, "text_tool_format", "plain") == "json_schema"

    def load_view(self) -> Any:
        task, effects = self.durable.task_facts(self.sink.run_id)
        return load_session_view(checkpoint_entries(task, effects, session_id=self.sink.session_id),
                                 session_id=self.sink.session_id, run_id=self.sink.run_id)

    def cancelled(self) -> bool:
        task = self.durable.task(self.sink.run_id)
        flag = self.transport.stop_flag
        return bool(task.state.get("cancel_requested")) or (flag is not None and bool(flag.is_set()))

    def decision_inputs(self, view: Any) -> Any:
        from codey.operations.shell_approval_receipt import read_approval_command
        from codey.runtime.core.operation_reducer import RuntimeDecisionInputs
        from codey.runtime.effects.effect_records import compute_args_digest

        task = self.durable.task(self.sink.run_id)
        state = view.state
        question = task.state.get("question", {})
        decision = self.decision(state) if state.work_ref else {}
        approval = None
        if state.leaf == positions.LEAF_TOOL_EFFECT_PENDING:
            for identity in task.state.get("approvals", {}):
                record = self.durable.approval(identity)
                command = read_approval_command(self.sink.managed_outputs, record)
                matching = [effect for effect in view.effects if effect.intent.effect_category == "tool_call"
                    and effect.settlement is None and effect.intent.turn == command.get("turn")
                    and effect.intent.tool_index == command.get("tool_index")
                    and effect.intent.call_id == command.get("call_id", "")
                    and effect.intent.tool_name == command.get("tool_name")
                    and effect.intent.args_digest == compute_args_digest(command.get("tool_args"))]
                if matching:
                    if approval is not None or len(matching) != 1:
                        raise RecoveryFailed("ambiguous approval continuation")
                    approval = record
        return RuntimeDecisionInputs(cancel_requested=self.cancelled(), closing_intent=decision.get("purpose") == "closing",
            question_answered=decision.get("purpose") == "question" and question.get("id") == decision.get("question_id")
                and question.get("state") == "answered",
            approval_ref=approval.approval_id if approval is not None else "",
            approval_ready=approval is not None and str(approval.status) in {"execution_settled", "denied", "expired", "cancelled"},
            approval_outcome_unknown=approval is not None and str(approval.status) == "execution_claimed")

    def reconcile_approval(self, state: Any, identity: str, *, cancelled: bool) -> None:
        from codey.operations.kernel_receipts import result_receipt_fields
        from codey.operations.shell_approval_receipt import read_approval_command, resolve_shell_approval
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.runtime.effects.effect_records import RuntimeEffectSettlement, compute_args_digest

        record = self.durable.approval(identity)
        cancelled = cancelled or str(record.status) in {"expired", "cancelled"}
        command = read_approval_command(self.sink.managed_outputs, record)
        payload = resolve_shell_approval(self.durable, self.sink.managed_outputs, identity)
        if payload is None and not cancelled:
            raise RecoveryFailed("approval has no reliable execution receipt")
        call = ToolCall(command["tool_name"], command["tool_args"], call_id=command.get("call_id", ""))
        matching = [effect for effect in self.load_view().effects if effect.intent.effect_category == "tool_call"
            and effect.intent.turn == command["turn"] and effect.intent.tool_index == command["tool_index"]
            and effect.intent.args_digest == compute_args_digest(call.args) and effect.intent.call_id == call.call_id]
        if len(matching) != 1 or record.task_id != self.sink.run_id or record.conversation_id != self.sink.session_id:
            raise RecoveryFailed("approval does not identify its original invocation")
        effect = matching[0]
        if effect.settlement is not None:
            return
        if payload is None:
            if str(record.status) not in {"requested", "approved", "cancelled", "expired"}:
                raise RecoveryFailed("approval execution outcome is unknown")
            text, ok, code = "ERROR: command cancelled before execution", False, None
        else:
            text, ok, code = payload["model_text"], payload["ok"], payload["exit_code"]
        result = ToolResult(call, text, ok=ok, audit={"exit_code": code})
        receipt = RuntimeEffectSettlement(effect_id=effect.intent.effect_id, effect_category="tool_call",
            session_id=self.sink.session_id, run_id=self.sink.run_id, status="ok" if ok else "error",
            error_code="" if ok else "cancelled" if payload is None else payload["error_code"] or "shell_error",
            replay_class=effect.intent.replay_class, exit_code=code,
            **result_receipt_fields(result, store=self.sink.managed_outputs,
                session_id=self.sink.session_id, run_id=self.sink.run_id, effect_id=effect.intent.effect_id))
        self.sink.mutations.settle_tool_effect(self.sink.session_id, self.sink.run_id, receipt)

    def advance(self, state: Any, leaf: str, *, data: dict[str, Any] | None = None,
                logical_turn: int | None = None, counters: RuntimeExecutionCounters | None = None) -> None:
        artifact = None
        if data is not None and (not state.work_ref or self.decision(state) != data):
            ref = self.sink.managed_outputs.write_json_artifact(
                session_id=self.sink.session_id, run_id=self.sink.run_id,
                payload={"schema_version": 1, "kind": "kernel_decision", "data": data})
            artifact = self.sink.managed_outputs.json_artifact_record(ref, owner_task_id=self.sink.run_id, owner_effect_id=None,
                                                                    metadata={"kind": "kernel_decision"})
        self.sink.mutations.advance_execution(self.sink.session_id, self.sink.run_id, expected=state,
            leaf=leaf, artifact=artifact, logical_turn=logical_turn, counters=counters)

    def decision(self, state: Any) -> dict[str, Any]:
        artifact = self.durable.artifact(state.work_ref)
        if (artifact.owner_task_id != self.sink.run_id or artifact.owner_effect_id is not None
                or artifact.metadata.get("kind") != "kernel_decision"):
            raise RecoveryFailed("kernel decision ownership mismatch")
        raw = thaw_json(artifact.metadata["ref"])
        if not isinstance(raw, dict) or raw.get("handle") != artifact.handle or raw.get("sha256") != artifact.sha256:
            raise RecoveryFailed("kernel decision digest reference mismatch")
        envelope = self.sink.managed_outputs.read_json_artifact(raw)
        if (not isinstance(envelope, dict) or set(envelope) != {"schema_version", "kind", "data"}
                or envelope["schema_version"] != 1 or envelope["kind"] != "kernel_decision"
                or not isinstance(envelope["data"], dict)):
            raise RecoveryFailed("invalid kernel decision")
        from codey.operations.kernel_control import validate_decision

        try:
            return validate_decision(envelope["data"])
        except (ValueError, TypeError, KeyError) as exc:
            raise RecoveryFailed(f"invalid committed control decision: {exc}") from exc

    def response(self, state: Any) -> Any:
        response_ref = self.response_reference(state)
        if response_ref not in self.responses:
            self.responses[response_ref] = self.sink.load_response(self.session, response_ref, window_ref=self.window)
        received = self.responses[response_ref]
        self.session.turn = received.logical_turn
        self.effect_scope = received.effect_scope
        return received

    def response_reference(self, state: Any) -> str:
        if state.response_ref:
            return str(state.response_ref)
        from codey.operations.provider_receipts import REQUEST_KIND, read_provider_artifact, request_artifact_for_effect

        effect = not_sent_continuation_effect(self.load_view())
        request = read_provider_artifact(self.sink.managed_outputs, request_artifact_for_effect(self.durable, effect),
            kind=REQUEST_KIND, session_id=self.sink.session_id, run_id=self.sink.run_id, effect_id=effect.intent.effect_id)
        reference = request.get("source_response_ref")
        if not isinstance(reference, str) or not reference or request.get("work_ref") != state.work_ref:
            raise RecoveryFailed("provider request lost its original continuation")
        return reference

    def restore_facts(self) -> None:
        from codey.operations.kernel_facts import project_execution_record, project_result_observation
        from codey.operations.kernel_receipts import restore_result_receipt

        for effect in RuntimeEffectStore(durable=self.durable).load_effects(self.sink.session_id, self.sink.run_id):
            identity = effect.intent.effect_id
            if effect.intent.effect_category == "provider_send":
                self.restore_completion_proposal(effect)
                continue
            if self.session.task_kind in {"research", "evidence_followup"}:
                from codey.operations.task_session import turn_effect_id
                scoped_run = f"{self.transport.run_id}:{self.effect_scope}" if self.effect_scope else self.transport.run_id
                if identity != turn_effect_id(scoped_run, effect.intent.turn, effect.intent.tool_index):
                    continue
            if (effect.intent.effect_category != "tool_call" or effect.settlement is None
                    or identity in self.session.executed or identity in self.session.restored_effect_ids):
                continue
            result = restore_result_receipt(effect, store=self.sink.managed_outputs,
                session_id=self.sink.session_id, run_id=self.sink.run_id, project_path=self.execution.project_path,
                workspace_store=self.execution.workspace_revision_store, ignored_paths=self.execution.workspace_ignored_paths)
            staged = self.session.clone_projection()
            ledger = getattr(self.execution.research_tools, "ledger", None)
            staged_ledger = ledger.clone() if ledger is not None else None
            code = effect.settlement.exit_code
            project_result_observation(staged, result.call, result, ok=result.ok, exit_code=code, research_ledger=staged_ledger)
            project_execution_record(staged, identity, result.call, result, ok=result.ok, exit_code=code)
            staged._memory_results[identity] = result
            staged.restored_effect_ids.add(identity)
            if staged.research_acquisition_limits is not None:
                category = "search" if result.call.name == "web_search" else "open" if result.call.name in {
                    "open_url", "open_result", "reopen_source", "open_hit"} else ""
                if category:
                    staged.research_acquisition_slots[(effect.intent.turn, effect.intent.tool_index)] = category
            if ledger is not None:
                ledger.commit_projection(staged_ledger)
                from codey.operations.research_continuation import restore_note_proposals
                restore_note_proposals(self.execution.research_tools, result.canonical)
            self.session.commit_projection(staged)
        tools = self.execution.research_tools
        if tools is not None:
            from codey.operations.research_continuation import ResearchContinuation
            ResearchContinuation.project_urls(tools)

    def restore_completion_proposal(self, effect: Any) -> None:
        """Rebuild proposal metadata from its original response, without a gate."""
        from codey.operations.provider_receipts import (
            REQUEST_KIND,
            RESPONSE_KIND,
            read_provider_artifact,
            request_artifact_for_effect,
            restore_snapshot,
        )
        from codey.providers.response_record import decode_reply

        identity = effect.intent.effect_id
        if identity in self.projected_provider_effects or effect.intent.phase != self.sink.phase:
            return
        artifacts = [row for row in self.durable.artifacts_for_effect(identity)
                     if row.metadata.get("kind") == RESPONSE_KIND]
        if not artifacts:
            return
        if len(artifacts) != 1:
            raise RecoveryFailed("provider proposal has conflicting response facts")
        request = read_provider_artifact(self.sink.managed_outputs, request_artifact_for_effect(self.durable, effect),
            kind=REQUEST_KIND, session_id=self.sink.session_id, run_id=self.sink.run_id, effect_id=identity)
        if (not isinstance(request, dict) or type(request.get("effect_scope")) is not str
                or type(request.get("close_reply")) is not bool):
            raise RecoveryFailed("provider proposal has no original invocation scope")
        if request.get("effect_scope") != self.effect_scope or request.get("close_reply"):
            self.projected_provider_effects.add(identity)
            return
        raw = read_provider_artifact(self.sink.managed_outputs, artifacts[0], kind=RESPONSE_KIND,
            session_id=self.sink.session_id, run_id=self.sink.run_id, effect_id=identity)
        snapshot = restore_snapshot(request.get("snapshot"))
        if snapshot.policy != self.session.policy:
            raise RecoveryFailed("completion proposal authorization differs from its original request")
        reply = decode_reply(raw)
        plan = kernel_protocol.normalize_turn(decode_text_turn(reply) if isinstance(reply, str) else reply, snapshot=snapshot)
        if not plan.protocol_error and plan.control is not None and plan.control.kind == "done":
            self.session.last_done_text = plan.control.body or ""
            self.session.last_done_args = dict(plan.control_args)
        self.projected_provider_effects.add(identity)

    def results(self, view: Any, batch_id: str) -> list[Any]:
        self.restore_facts()
        batch = next(batch for batch in view.batches if batch.intent.batch_id == batch_id)
        return [self.session._memory_results[identity] for identity in batch.intent.tool_refs]

    def freeze(self, state: Any, snapshot: Any, *, turn: int, prompt: str = "",
               messages: list[ProviderToolResult] | None = None, acknowledge: bool = False,
               counters: RuntimeExecutionCounters | None = None,
               text_result_spans: list[dict[str, Any]] | None = None) -> None:
        from codey.operations.kernel_driver import prepare_provider_request

        json_contract = self.request_scoped_context
        if not self.native and not json_contract and state.leaf not in {positions.LEAF_WRITER_RUNNING, positions.LEAF_REPAIR_RUNNING}:
            prompt = kernel_prompt._text_request_prompt(prompt, snapshot)
        self.snapshots[turn] = snapshot
        self.sink.bind_request(snapshot, logical_turn=turn, window_ref=self.window)
        self.sink._request_context["effect_scope"] = self.effect_scope
        self.sink._request_context["close_reply"] = acknowledge
        self.sink._request_context["delivery_purpose"] = self.decision(state).get("purpose", "") if state.work_ref else ""
        self.sink._request_context["work_ref"] = state.work_ref
        self.sink._request_context["source_response_ref"] = (
            self.response_reference(state) if state.response_ref or state.work_ref
            or state.leaf == positions.LEAF_TOOL_DELIVERY_PENDING else "")
        tools = tools_from_specs(snapshot.frozen_specs)
        if self.native and messages:
            method = "acknowledge_tool_results" if acknowledge else "send_tool_results"
            args: tuple[Any, ...] = (messages, tools)
            if self.window is not None:
                method += "_bound"
                args = (messages, self.window, tools)
        else:
            method, args = ("send_turn", (prompt, tools)) if self.native else ("send", (prompt,))
        options: dict[str, Any] = {}
        if json_contract:
            from codey.providers.text_tool_schema import text_tool_schema

            options = {"tool_call_json": True, "tool_call_schema": text_tool_schema(tools),
                       "tool_call_contract": kernel_prompt._text_request_prompt("", snapshot),
                       "tool_call_observations": task_working_context(self.session)}
            if text_result_spans:
                options["tool_call_results"] = text_result_spans
        prepare_provider_request(self.sink, method, args, options, counters=(state.counters, counters or state.counters))

    def prepare_turn(self, state: Any) -> None:
        self.restore_facts()
        turn = state.logical_turn + 1
        if turn > self.session.max_turns:
            self.close(state, KernelResult(False, self.session.last_done_text or "max turns reached", turn - 1, "max_turns"))
            return
        from codey.operations.knowledge_context import knowledge_background

        recalled = knowledge_background(self.execution.research_tools, self.session, str(self.transport.user_task or ""))
        prepared = prepare_kernel_turn(self.session, user_task=str(self.transport.user_task or ""),
            context_text="\n\n".join(part for part in (self.transport.context_text, recalled) if part),
            native=self.native, task_guidance=self.request.task_guidance,
            completion_context=self.completion_context,
            request_scoped_contract=not self.native and getattr(self.provider, "text_tool_format", "plain") == "json_schema")
        prompt = prepared.prompt
        if state.logical_turn and self.boundary_text():
            prompt += "\n\nNew user input at provider boundary:\n" + self.boundary_text()
        admission = admit_ghost_context(prompt, self.transport.ghost_sources, request_scope=self.transport.context_scope,
            require_all=not self.attempt_gate_bound, admit_hints=self.transport.admit_hints)
        if not self.attempt_gate_bound:
            record_outbound(self.observation.trace_recorder, prompt, admission=admission)
        progress = KernelProgress(self.transport.stagnant_turns, snapshot=state.counters.progress)
        if not self.request_scoped_context:
            prompt = "\n\n".join(part for part in (prompt, task_working_context(self.session)) if part)
        self.freeze(state, prepared.snapshot, turn=turn, prompt=prompt,
                    counters=replace(state.counters, progress=progress.snapshot()))

    def interpret(self, state: Any) -> None:
        received = self.response(state)
        reply, snapshot, turn = received.reply, received.snapshot, received.logical_turn
        from codey.operations.provider_receipts import REQUEST_KIND, read_provider_artifact, request_artifact_for_effect

        artifact = self.durable.artifact(state.response_ref)
        effect = next(row for row in self.load_view().effects if row.intent.effect_id == artifact.owner_effect_id)
        original = read_provider_artifact(self.sink.managed_outputs, request_artifact_for_effect(self.durable, effect),
            kind=REQUEST_KIND, session_id=self.sink.session_id, run_id=self.sink.run_id, effect_id=effect.intent.effect_id)
        if original.get("close_reply"):
            if original.get("work_ref") != state.work_ref:
                raise RecoveryFailed("protocol close response lost its original outcome")
            self.advance(state, positions.LEAF_CLOSING)
            return
        if turn > self.session.max_turns:
            self.close(state, KernelResult(False, self.session.last_done_text or "max turns reached",
                                           self.session.max_turns, "max_turns"))
            return
        kernel_events._emit_turn_event(self.observation.on_event, turn, reply)
        if self.native and reply.finish is TurnFinish.OUTPUT_LIMIT:
            if state.counters.length_continuation_used:
                self.close(state, KernelResult(False, "local provider emitted finish_reason=length twice", turn, "provider_failure"))
                return None
            if not reply.tool_calls:
                self.retry(state, "Your previous response was truncated before a tool call. Continue the task with the next required tool call, or call done(summary) if the verified task is complete. Do not output ordinary explanation.",
                           counters=replace(state.counters, length_continuation_used=True))
                return
        decoded = decode_text_turn(reply) if isinstance(reply, str) else reply
        plan = kernel_protocol.normalize_turn(decoded, snapshot=snapshot)
        record_turn(self.observation.trace_recorder, phase=self.session.task_kind, turn=turn,
                    snapshot=snapshot, plan=plan, native=self.native)
        if plan.protocol_error:
            invalid = state.counters.invalid_turns + 1
            threshold = self.transport.stagnant_turns
            if threshold is not None and invalid >= max(1, threshold):
                self.advance(state, positions.LEAF_CLOSING, counters=replace(state.counters, invalid_turns=invalid),
                    data={"purpose": "closing", "result": json_value(KernelResult(False,
                        f"stopped after {invalid} invalid tool requests: {plan.protocol_error}", turn, "protocol"))})
                return
            prompt = kernel_prompt._repair_prompt(plan, snapshot=snapshot, native=self.native)
            self.retry(state, prompt, error=plan.protocol_error, counters=replace(state.counters, invalid_turns=invalid))
            return
        kind = getattr(plan.control, "kind", "")
        if kind == "ask_user":
            from codey.task.questions import question_args
            question = question_args(dict(plan.control_args))
            identity = f"{self.transport.run_id}:{self.effect_scope}" if self.effect_scope else self.transport.run_id
            question_id = "question_" + hashlib.sha256(f"{identity}:{turn}".encode()).hexdigest()[:24]
            self.advance(state, positions.LEAF_CONTROL_READY, data={"purpose": "question", "question": question,
                "question_id": question_id, "result": json_value(KernelResult(False, question["question"], turn, "question"))})
            return
        self.advance(state, positions.LEAF_COMPLETION_PENDING if kind == "done" else positions.LEAF_PLAN_READY,
                     counters=replace(state.counters, invalid_turns=0))

    def retry(self, state: Any, prompt: str, *, error: str = "", counters: RuntimeExecutionCounters | None = None) -> None:
        self.advance(state, positions.LEAF_CONTROL_READY, data={"purpose": "retry", "prompt": prompt,
            "error": error, "counters": (counters or state.counters).to_payload()})

    def plan(self, state: Any) -> Any:
        received = self.response(state)
        reply = decode_text_turn(received.reply) if isinstance(received.reply, str) else received.reply
        plan = kernel_protocol.normalize_turn(reply, snapshot=received.snapshot)
        if plan.protocol_error:
            raise RecoveryFailed("accepted plan no longer parses under its captured contract")
        return plan

    def execute_tools(self, state: Any) -> KernelResult | None:
        self.restore_facts()
        received, plan = self.response(state), self.plan(state)
        calls, turn = list(plan.calls), received.logical_turn
        if not received.snapshot.implementation_compatible:
            raise RecoveryFailed("captured tool implementation is unavailable or incompatible")
        if state.leaf == positions.LEAF_PLAN_READY:
            approval = wiring._approval_stop(calls, project_path=self.execution.project_path,
                on_shell_request=self.observation.on_shell_request, turn=turn,
                run_id=f"{self.transport.run_id}:{self.effect_scope}" if self.effect_scope else self.transport.run_id,
                intent_sink=self.sink, policy=self.session.policy, permission_profile=self.execution.permission_profile,
                provider_id=self.transport.provider_id, window_ref=self.window, shell=self.session.shell,
                durable=self.durable, managed_outputs=self.sink.managed_outputs, task_id=self.sink.run_id)
            if approval is not None:
                if self.cancelled():
                    from codey.runtime.core.cancellation import TaskCancelled
                    raise TaskCancelled("stopped while publishing approval")
                # The saved approval and original tool intents now determine
                # the boundary and suspension through the same reducer.
                return None
        if self.execution.ensure_workspace_writer is not None and any(call.name in {"edit", "run"} for call in calls):
            try:
                self.execution.ensure_workspace_writer()
            except Exception as exc:
                from codey.workspace.changes import ProjectWriteBusy
                self.close(state, KernelResult(False, f"workspace write blocked: {exc}", turn,
                    "project_write_busy" if isinstance(exc, ProjectWriteBusy) else "controller_failure"))
                return None
        kernel_events._emit_tool_starts(self.observation.on_event, self.session, turn, calls)
        result = wiring._call_execute_turn(self.session, calls, self.execution.executors, self.transport.run_id,
            self.effect_scope, turn, self.execution.project_path, self.execution.tool_fns,
            self.execution.research_tools, self.execution.change_tracker, self.sink.managed_outputs,
            self.sink.session_id, self.execution.permission_profile, self.session._memory_results,
            self.sink, received.snapshot.allowed, wiring._outer_evidence_for_context(self.completion_context),
            self.execution.workspace_ignored_paths, self.execution.workspace_revision_store,
            turn, True, self.transport.stop_flag, durable=self.durable, snapshot=received.snapshot,
            on_event=self.observation.on_event)
        if isinstance(result, KernelResult):
            raise RecoveryFailed(result.summary)
        return None

    def observe_progress(self, view: Any, action: Any) -> None:
        results = self.results(view, action.delivery_batch_id)
        self.session.turn = view.state.turn
        progress = KernelProgress(self.transport.stagnant_turns, snapshot=view.state.counters.progress)
        previous = progress.snapshot()
        progress.observe(results, self.session, batch_id=action.delivery_batch_id)
        self.sink.delivery_batch_id = action.delivery_batch_id
        self.sink.record_progress(previous, progress.snapshot())

    def deliver_results(self, view: Any, action: Any) -> None:
        state = view.state
        results = self.results(view, action.delivery_batch_id)
        self.sink.delivery_batch_id = action.delivery_batch_id
        turn = state.turn
        kernel_events._emit_tool_results(self.observation.on_event, self.session, results,
            run_id=f"{self.transport.run_id}:{self.effect_scope}" if self.effect_scope else self.transport.run_id,
            turn=turn)
        progress = KernelProgress(self.transport.stagnant_turns, snapshot=state.counters.progress)
        if self.cancelled() or progress.stopped or turn >= self.session.max_turns:
            reason = "stopped" if self.cancelled() else "no_progress" if progress.stopped else "max_turns"
            summary = {"stopped": "stopped", "no_progress": "stopped after repeated tool results without progress",
                       "max_turns": self.session.last_done_text or "max turns reached"}[reason]
            from codey.operations.kernel_control import no_progress_result
            self.close(state, no_progress_result(self.session, turn) if reason == "no_progress"
                       else KernelResult(False, summary, turn, reason))
            return
        spans: list[dict[str, Any]] = []
        advance = wiring._advance_after_results(self.native, results, self.session, None,
            completion_context=self.completion_context, boundary_prompt=self.boundary_text(), text_result_spans=spans)
        if isinstance(advance, KernelResult):
            self.close(state, advance)
            return
        messages, prompt = advance
        observations = "" if self.request_scoped_context else task_working_context(self.session)
        if self.native and messages:
            messages[-1] = replace(messages[-1], user_content="\n\n".join(
                part for part in (observations, messages[-1].user_content) if part))
        else:
            # Keep the latest result after historical observations: previous
            # findings must not displace the passage currently being inspected.
            prompt = "\n\n".join(part for part in (observations, prompt) if part)
            shift = len(observations) + 2 if observations else 0
            spans = [{**span, "start": span["start"] + shift, "end": span["end"] + shift} for span in spans]
        snapshot = kernel_protocol.build_turn_snapshot(self.session, native=self.native)
        self.freeze(state, snapshot, turn=turn + 1, prompt=prompt, messages=messages, text_result_spans=spans)

    def boundary_text(self) -> str:
        task = self.durable.task(self.sink.run_id)
        receipt = task.state.get("generation_boundary_receipt", {})
        return "\n\n".join(str(self.durable.submission(identity).content.get("text", ""))
                             for identity in receipt.get("submission_ids", ()))

    def boundary(self, action: Any) -> None:
        from codey.operations.generation_boundary import GenerationBoundaryRequest
        if self.observation.on_boundary is None:
            raise RecoveryFailed("an admitted provider generation requires a durable boundary callback")
        self.observation.on_boundary(GenerationBoundaryRequest(action.reason))

    def close(self, state: Any, result: KernelResult) -> None:
        self.advance(state, positions.LEAF_CLOSING, data={"purpose": "closing", "result": json_value(result)})
