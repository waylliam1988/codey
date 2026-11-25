"""Resource-bound execution of the operation reducer's concrete actions.

The operation checkpoint owns position; effects own admission and receipts.
This object contains dependencies and rebuildable caches only.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from codey.durable.model import ArtifactRecord, thaw_json
from codey.knowledge.model import canonical_json
from codey.operations import kernel_actions as wiring
from codey.operations import kernel_events, kernel_prompt
from codey.operations.kernel import kernel_protocol
from codey.operations.kernel.kernel_protocol import ReceivedProviderTurn
from codey.operations.kernel.turn_contract import TurnSnapshot
from codey.operations.kernel_capabilities import KernelCapabilityView
from codey.operations.kernel_contracts import (
    ClosingDecision,
    KernelDecision,
    KernelResult,
    KernelRunRequest,
    QuestionDecision,
    RetryDecision,
)
from codey.operations.kernel_errors import RecoveryFailed
from codey.operations.kernel_preparation import capture_turn_snapshot, prepare_kernel_turn
from codey.operations.kernel_progress import KernelProgress, ProjectCheckCycle, project_check_cycle
from codey.operations.kernel_trace import record_outbound, record_turn
from codey.operations.project.edit_base import bound_symbol_retry, is_compact_symbol_retry
from codey.operations.project.project_prompt_context import prepare_coding_context
from codey.operations.provider_request_context import ProviderRequestContext
from codey.operations.task_guidance import task_result_context, task_working_context
from codey.operations.task_session import TaskSession, turn_effect_id
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolResult, TurnFinish, tools_from_specs
from codey.providers.requests import (
    AcknowledgeToolResultsBoundRequest,
    AcknowledgeToolResultsRequest,
    ProviderRequest,
    ProviderRequestOptions,
    SendTextRequest,
    SendToolResultsBoundRequest,
    SendToolResultsRequest,
    SendTurnRequest,
)
from codey.providers.task_prompt import TaskPromptSpan
from codey.providers.text_result_views import TextResultSpan
from codey.runtime.core import operation_payload
from codey.runtime.core.models import ToolCall, ToolPlan, ToolResult
from codey.runtime.core.operation_payload import RuntimeExecutionCounters, RuntimeOperationState
from codey.runtime.core.operation_reducer import RuntimeAction, RuntimeDecisionInputs
from codey.runtime.core.tool_resources import tool_resources_for
from codey.runtime.effects.effect_records import RuntimeEffectProjection
from codey.runtime.log.session_view import (
    SessionView,
    driver_for_state,
    not_sent_continuation_effect,
)
from codey.runtime.write.checkpoint import view_from_checkpoint
from codey.types.json_value import JsonObject, decode_json_object, require_json_string
from codey.workspace.coding_context import render_coding_context
from codey.workspace.context_admission import admit_context

if TYPE_CHECKING:
    from codey.durable.session import DurableSession
    from codey.operations.progress_comparison import ProjectCheckComparison
    from codey.operations.task_effects import KernelEffectSink
    from codey.research.tools import ResearchTools


class _ResearchProjector(Protocol):
    def project_urls(self, tools: ResearchTools) -> None: ...


def _host_acquisition_result(result: ToolResult) -> bool:
    from codey.research.primary_sources import PRIMARY_ACQUISITION_IDS

    return ((result.call.name == "web_search" and not result.call.call_id)
            or result.call.call_id in PRIMARY_ACQUISITION_IDS)


def _research_effect_ids(effects: Sequence[RuntimeEffectProjection], run_id: str, scope: str) -> set[str]:
    scoped_run = f"{run_id}:{scope}" if scope else run_id
    own = tuple(effect.intent for effect in effects
                if effect.intent.run_id == run_id and effect.intent.effect_category == "tool_call")
    ids = {item.effect_id for item in own if item.effect_id == turn_effect_id(scoped_run, item.turn, item.tool_index)}
    while True:
        children = {item.effect_id for item in own if item.parent_effect_id in ids}
        if children <= ids:
            return ids
        ids.update(children)


class KernelContext:
    def __init__(self, session: TaskSession, request: KernelRunRequest) -> None:
        self.session, self.request = session, request
        self.transport, self.execution, self.observation = request.transport, request.execution, request.observation
        # Compatibility callers replace the observation sink during restart;
        # production builders place it in execution. Prefer an explicitly
        # supplied observation replacement when both are present.
        admitted_sink: KernelEffectSink | None = self.execution.intent_sink
        if admitted_sink is None:
            raise ValueError("kernel execution requires an admitted durable operation and managed output storage")
        _ = admitted_sink.managed_outputs
        self.sink: KernelEffectSink = admitted_sink
        self.project_check_comparisons: dict[str, ProjectCheckComparison | None] = {}
        self.durable: DurableSession = admitted_sink.durable
        admitted = self.load_view().state
        if admitted is None:
            raise ValueError("kernel execution requires an operation checkpoint")
        self.sink.phase = driver_for_state(admitted)
        session.max_turns = min(session.max_turns, max(0, admitted.turn_budget - admitted.turns_used))
        self.effect_scope = self.transport.effect_scope
        self.provider = self.transport.provider
        self.capabilities = KernelCapabilityView.from_provider(
            self.provider, provider_id=self.transport.provider_id, fallback_window_ref=self.transport.window_ref)
        self.native = self.capabilities.native_tools
        self.window = self.capabilities.window_ref
        self.completion_context = wiring._completion_context_with_ignores(
            self.observation.completion_context, self.execution.workspace_ignored_paths)
        from functools import partial

        from codey.operations.tool_result_reader import ReceiptReader, resolve_receipt_quote

        self.completion_context = replace(self.completion_context, resolve_quote=partial(resolve_receipt_quote,
            ReceiptReader(session, self.sink.managed_outputs, self.sink.session_id, self.sink.run_id), durable=self.durable))
        from codey.operations.browser.browser_progress import restore_browser_progress
        from codey.operations.browser.browser_resources import build_task_browser
        from codey.toolchain.tool_spec import tool_specs

        if ((session.policy.allows("web.read") and self.execution.research_tools is not None)
                or any(spec.executor == "browser" and session.policy.allows(spec.grant) for spec in tool_specs().values())):
            browser = build_task_browser(self.sink.run_id, tool_resources_for(self.durable),
                                         self.execution.research_tools, browser=self.execution.browser_session)
            self.execution = replace(self.execution, browser_session=browser)
            restore_browser_progress(browser, durable=self.durable, store=self.sink.managed_outputs, run_id=self.sink.run_id)
        if self.execution.research_tools is not None:
            tools = self.execution.research_tools
            tools.direct_urls = session.required_source_urls
            self.completion_context = replace(
                self.completion_context,
                research_ledger=tools.ledger,
                knowledge_store=tools.store,
                knowledge_project=tools.project,
                knowledge_session_id=tools.session_id,
            )
        self.responses: dict[str, ReceivedProviderTurn] = {}
        self.projected_provider_effects: set[str] = set()
        self.snapshots: dict[int, TurnSnapshot] = {}
        self.attempt_gate_bound = wiring._bind_attempt_gate(self.provider, self.transport, self.observation.trace_recorder,
            observe_memory=self.observe_memory_attempt if self.transport.memory_sources else None)

    def observe_memory_attempt(self, payload: JsonObject, reason: str) -> None:
        from codey.operations.memory_routing import record_memory_attempt

        record_memory_attempt(self.durable, self.sink.managed_outputs, session_id=self.sink.session_id,
            run_id=self.sink.run_id, sources=self.transport.memory_sources, payload=payload, reason=reason)

    def restore_context(self, payload: ProviderRequestContext) -> None:
        self.transport = replace(self.transport, context_text=payload.context_text, memory_sources=payload.memory_sources,
                                 memory_traces=payload.memory_traces)
        self.attempt_gate_bound = wiring._bind_attempt_gate(self.provider, self.transport, self.observation.trace_recorder,
            observe_memory=self.observe_memory_attempt if self.transport.memory_sources else None)

    @property
    def request_scoped_context(self) -> bool:
        return not self.native and (self.capabilities.text_tool_format == "json_schema"
                                    or self.capabilities.request_scoped_tool_context)

    @property
    def request_scoped_observations(self) -> bool:
        return self.request_scoped_context or self.capabilities.request_scoped_tool_context

    def load_view(self) -> SessionView:
        task, effects = self.durable.task_facts(self.sink.run_id)
        return view_from_checkpoint(task, effects, session_id=self.sink.session_id)

    def cancelled(self) -> bool:
        task = self.durable.task(self.sink.run_id)
        flag = self.transport.stop_flag
        return bool(task.state.get("cancel_requested")) or (flag is not None and bool(flag.is_set()))

    def decision_inputs(self, view: SessionView) -> RuntimeDecisionInputs:
        from codey.operations.shell_approval_receipt import read_approval_command
        from codey.runtime.effects.effect_records import compute_args_digest

        task = self.durable.task(self.sink.run_id)
        state = view.state
        if state is None:
            raise RecoveryFailed("kernel operation has no checkpoint state")
        question_raw = task.state.get("question")
        question: Mapping[str, object] = question_raw if isinstance(question_raw, Mapping) else dict[str, object]()
        approvals_raw = task.state.get("approvals")
        approvals: Mapping[str, object] = approvals_raw if isinstance(approvals_raw, Mapping) else dict[str, object]()
        decision = self.decision(state) if state.work_ref else None
        approval = None
        if state.leaf == operation_payload.LEAF_TOOL_EFFECT_PENDING:
            for identity in approvals:
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
        return RuntimeDecisionInputs(cancel_requested=self.cancelled(), closing_intent=decision is not None and decision["purpose"] == "closing",
            question_answered=decision is not None and decision["purpose"] == "question" and question.get("id") == decision["question_id"]
                and question.get("state") == "answered",
            approval_ref=approval.approval_id if approval is not None else "",
            approval_ready=approval is not None and str(approval.status) in {"execution_settled", "denied", "expired", "cancelled"},
            approval_outcome_unknown=approval is not None and str(approval.status) == "execution_claimed")

    def reconcile_approval(self, state: RuntimeOperationState, identity: str, *, cancelled: bool) -> None:
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
        from codey.runtime.core.execution_receipt import ExecutionReceipt

        execution = ExecutionReceipt.from_payload(payload["execution"]) if payload is not None else None
        result = ToolResult(call, text, ok=ok,
            truncated=payload["truncated"] if payload is not None else False,
            audit=execution.audit_facts() if execution is not None else {"exit_code": code},
            canonical={"execution": execution.facts()} if execution is not None else {})
        receipt = RuntimeEffectSettlement(effect_id=effect.intent.effect_id, effect_category="tool_call",
            session_id=self.sink.session_id, run_id=self.sink.run_id, status="ok" if ok else "error",
            error_code="" if ok else "cancelled" if payload is None else payload["error_code"] or "shell_error",
            replay_class=effect.intent.replay_class, exit_code=code,
            **result_receipt_fields(result, store=self.sink.managed_outputs,
                session_id=self.sink.session_id, run_id=self.sink.run_id, effect_id=effect.intent.effect_id))
        self.sink.mutations.settle_tool_effect(self.sink.session_id, self.sink.run_id, receipt)

    def advance(self, state: RuntimeOperationState, leaf: str, *, data: KernelDecision | None = None,
                logical_turn: int | None = None, counters: RuntimeExecutionCounters | None = None,
                audit: ArtifactRecord | None = None) -> None:
        artifact = None
        if data is not None and (not state.work_ref or self.decision(state) != data):
            ref = self.sink.managed_outputs.write_json_artifact(
                session_id=self.sink.session_id, run_id=self.sink.run_id,
                payload={"schema_version": 2, "kind": "kernel_decision", "data": data})
            artifact = self.sink.managed_outputs.json_artifact_record(ref, owner_task_id=self.sink.run_id, owner_effect_id=None,
                                                                    metadata={"kind": "kernel_decision"})
        self.sink.mutations.advance_execution(self.sink.session_id, self.sink.run_id, expected=state,
            leaf=leaf, artifact=artifact, logical_turn=logical_turn, counters=counters,
            audit_artifacts=(audit,) if audit is not None else ())

    def decision(self, state: RuntimeOperationState) -> KernelDecision:
        artifact = self.durable.artifact(state.work_ref)
        if (artifact.owner_task_id != self.sink.run_id or artifact.owner_effect_id is not None
                or artifact.metadata.get("kind") != "kernel_decision"):
            raise RecoveryFailed("kernel decision ownership mismatch")
        raw = decode_json_object(thaw_json(artifact.metadata["ref"]))
        if not isinstance(raw, dict) or raw.get("handle") != artifact.handle or raw.get("sha256") != artifact.sha256:
            raise RecoveryFailed("kernel decision digest reference mismatch")
        raw_envelope: object = self.sink.managed_outputs.read_json_artifact(dict[str, object](raw))
        envelope = decode_json_object(raw_envelope)
        if (not isinstance(envelope, dict) or set(envelope) != {"schema_version", "kind", "data"}
                or type(envelope["schema_version"]) is not int or envelope["schema_version"] not in (1, 2)
                or envelope["kind"] != "kernel_decision"
                or not isinstance(envelope["data"], dict)):
            raise RecoveryFailed("invalid kernel decision")
        from codey.operations.kernel_control import validate_decision

        try:
            decision: KernelDecision = validate_decision(
                envelope["data"], schema_version=envelope["schema_version"])
            return decision
        except (ValueError, TypeError, KeyError) as exc:
            raise RecoveryFailed(f"invalid committed control decision: {exc}") from exc

    def response(self, state: RuntimeOperationState) -> ReceivedProviderTurn:
        response_ref = self.response_reference(state)
        if response_ref not in self.responses:
            self.responses[response_ref] = self.sink.load_response(self.session, response_ref, window_ref=self.window)
        received = self.responses[response_ref]
        self.session.turn = received.logical_turn
        self.effect_scope = received.effect_scope
        return received

    def response_reference(self, state: RuntimeOperationState) -> str:
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
        from codey.operations.project.verification_history import rebuild_verifications, settled_projections

        effects = settled_projections(self.durable, self.sink.run_id)
        research_ids = (_research_effect_ids(effects, self.transport.run_id, self.effect_scope)
                        if self.session.task_kind in {"research", "evidence_followup"} else None)
        for effect in effects:
            identity = effect.intent.effect_id
            if effect.intent.tool_name == "_publish_research_answer":
                continue
            if effect.intent.effect_category == "provider_send":
                self.restore_completion_proposal(effect)
                continue
            if research_ids is not None and effect.intent.run_id == self.sink.run_id and identity not in research_ids:
                continue
            if (effect.intent.effect_category != "tool_call" or effect.settlement is None
                    or identity in self.session.executed or identity in self.session.restored_effect_ids):
                continue
            if effect.intent.run_id != self.sink.run_id and effect.settlement.status == "interrupted":
                from codey.operations.tool_tasks.reads import tool_execution_result

                restored = tool_execution_result(self.durable, self.durable.task(effect.intent.run_id),
                                                store=self.sink.managed_outputs)
                if restored is None:
                    continue
                result = restored[1]
            else:
                result = restore_result_receipt(effect, store=self.sink.managed_outputs,
                session_id=self.sink.session_id, run_id=effect.intent.run_id, project_path=self.execution.project_path,
                workspace_store=self.execution.workspace_revision_store, ignored_paths=self.execution.workspace_ignored_paths)
            staged = self.session.clone_projection()
            ledger = self.execution.research_tools.ledger if self.execution.research_tools is not None else None
            staged_ledger = ledger.clone() if ledger is not None else None
            code = effect.settlement.exit_code
            project_result_observation(staged, result.call, result, ok=result.ok, exit_code=code,
                                       research_ledger=staged_ledger, include_verification=False)
            project_execution_record(staged, identity, result.call, result, ok=result.ok, exit_code=code)
            staged._memory_results[identity] = result
            staged.restored_effect_ids.add(identity)
            if staged.research_acquisition_limits is not None:
                from codey.toolchain.tool_spec import is_source_read_tool

                category = "search" if result.call.name == "web_search" else "open" if is_source_read_tool(result.call.name) else ""
                if category:
                    staged.research_acquisition_slots[(effect.intent.turn, effect.intent.tool_index)] = category
            if ledger is not None:
                assert staged_ledger is not None
                ledger.commit_projection(staged_ledger)
                from codey.operations.research.research_continuation import restore_note_proposals
                tools = self.execution.research_tools
                if tools is not None:
                    restore_note_proposals(tools, result.canonical)
            self.session.commit_projection(staged)
        rebuild_verifications(self.session, durable=self.durable, owner_task_id=self.sink.run_id,
            store=self.sink.managed_outputs, project_path=self.execution.project_path,
            workspace_store=self.execution.workspace_revision_store, ignored_paths=self.execution.workspace_ignored_paths)
        # The outer review/receipt reads this rebuildable projection too.
        # Owned child results have no second parent tool event; project the
        # same formal verification observations used by the completion gate.
        from codey.operations.project_completion_checks import _evidence_with_session_facts

        if self.execution.workspace_revision_store is not None and self.execution.project_path is not None:
            current = self.execution.workspace_revision_store.current_state(
                self.execution.project_path, ignored_paths=self.execution.workspace_ignored_paths)
            self.session.set_workspace_state(current.revision, current.fingerprint)
            if self.completion_context.execution_evidence is not None:
                self.completion_context.execution_evidence.set_workspace_state(current.revision, current.fingerprint)
        _evidence_with_session_facts(self.completion_context.execution_evidence, self.session)
        tools = self.execution.research_tools
        if tools is not None:
            from codey.operations.research.research_continuation import ResearchContinuation
            projector: _ResearchProjector = ResearchContinuation
            projector.project_urls(tools)
        if self.execution.browser_session is not None:
            self.execution.browser_session.prepare_from_receipts(tuple(self.session._memory_results.values()))

    def restore_completion_proposal(self, effect: RuntimeEffectProjection) -> None:
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

    def results(self, view: SessionView, batch_id: str) -> list[ToolResult]:
        self.restore_facts()
        batch = next(batch for batch in view.batches if batch.intent.batch_id == batch_id)
        return [self.session._memory_results[identity] for identity in batch.intent.tool_refs]

    def request_snapshot(self, snapshot: TurnSnapshot, *,
                         delivered_results: Sequence[ToolResult] = ()) -> tuple[TurnSnapshot, str]:
        from codey.operations.kernel.request_preparation import prepare_request_snapshot

        return prepare_request_snapshot(self.session, snapshot, self.execution, self.completion_context,
            durable=self.durable, run_id=self.sink.run_id, managed_outputs=self.sink.managed_outputs,
            collect_memory=self.transport.collect_memory_candidates
                and (self.native or self.capabilities.text_tool_format == "json_schema"),
            delivered_results=delivered_results)

    def freeze(self, state: RuntimeOperationState, snapshot: TurnSnapshot, *, turn: int, prompt: str = "",
               messages: list[ProviderToolResult] | None = None, acknowledge: bool = False,
               counters: RuntimeExecutionCounters | None = None,
               text_result_spans: tuple[TextResultSpan, ...] = (),
               coding_context_prepared: bool = False, initial_source_batch: bool = False,
               handoff: str | None = None, task_span: TaskPromptSpan | None = None) -> None:
        from codey.operations.explanation_repair import active, repair_messages, repair_prompt, repair_snapshot
        from codey.operations.provider_request import prepare_provider_request

        explanation_only = not acknowledge and active((counters or state.counters).progress)
        if explanation_only:
            snapshot = repair_snapshot(snapshot)
            handoff = ""
            decision = self.decision(state) if state.leaf == operation_payload.LEAF_CONTROL_READY else None
            prompt = repair_prompt(self.session, self.completion_context, snapshot, native=self.native, decision=decision)
            text_result_spans = ()
            task_span = None
            coding_context_prepared = True
            messages = repair_messages(messages, prompt, rejected_control=state.leaf == operation_payload.LEAF_CONTROL_READY)
        elif not acknowledge:
            snapshot, handoff = self._request_check_handoff(snapshot, handoff)
        delivery_handoff = handoff if self.request_scoped_observations and not acknowledge else ""
        if handoff and not delivery_handoff:
            if self.native and messages:
                messages[-1] = replace(messages[-1], user_content="\n\n".join(
                    part for part in (messages[-1].user_content, handoff) if part))
            else:
                prompt = "\n\n".join(part for part in (prompt, handoff) if part)
        from codey.operations.tool_tasks.delivery import append_tool_task_notifications

        prompt, messages, notification_refs = append_tool_task_notifications(
            self.durable, owner_task_id=self.sink.run_id, store=self.sink.managed_outputs,
            results=self.session._memory_results, enabled=not acknowledge and not explanation_only,
            native=self.native, prompt=prompt, messages=messages)
        scoped_contract = self.request_scoped_context
        schema_constrained = (scoped_contract and self.capabilities.text_tool_format == "json_schema"
                              and not is_compact_symbol_retry(snapshot))
        scoped_tool_contract = (kernel_prompt._text_request_prompt("", snapshot, schema_constrained=schema_constrained)
                                if scoped_contract else "")
        if not self.native and not scoped_contract and state.leaf not in {operation_payload.LEAF_WRITER_RUNNING, operation_payload.LEAF_REPAIR_RUNNING}:
            prompt = kernel_prompt._text_request_prompt(prompt, snapshot)
        self.snapshots[turn] = snapshot
        self.sink.bind_request(snapshot, logical_turn=turn, window_ref=self.window,
            context_text=prompt if explanation_only else self.transport.context_text, memory_sources=self.transport.memory_sources,
            memory_traces=self.transport.memory_traces,
            effect_scope=self.effect_scope, close_reply=acknowledge,
            delivery_purpose=self.decision(state)["purpose"] if state.work_ref else "", work_ref=state.work_ref,
            source_response_ref=self.response_reference(state) if state.response_ref or state.work_ref
                or (state.leaf == operation_payload.LEAF_TOOL_DELIVERY_PENDING and not initial_source_batch) else "",
            tool_task_notifications=notification_refs)
        tools = tuple(tools_from_specs(snapshot.model_specs))
        options = ProviderRequestOptions(task_prompt_span=task_span,
            require_tool_call=self.native and not acknowledge and bound_symbol_retry(snapshot) is not None)
        coding = ""
        # Full-turn prompts already contain their prepared context. Native
        # continuations still need a fresh projection after tool execution.
        if self.native and not acknowledge and not coding_context_prepared:
            prepared = prepare_coding_context(self.session, completion_context=self.completion_context)
            coding = render_coding_context(prepared, native=True) if prepared is not None else ""
            if coding and coding not in prompt and not self.request_scoped_observations and messages:
                messages[-1] = replace(messages[-1], user_content="\n\n".join(
                    part for part in (messages[-1].user_content, coding) if part))
        if explanation_only:
            options = replace(options, tool_call_observations=prompt, tool_call_history_groups=1)
        elif self.request_scoped_observations and not (self.native and acknowledge):
            from codey.providers.response_record import json_value
            # The sampling schema is not prompt prose. The provider counts its
            # real wire envelope at admission; serializing it here counts a
            # second textual tool catalog that this request never sends.
            declaration = ((scoped_tool_contract or kernel_prompt._text_request_prompt("", snapshot))
                           if not self.native else canonical_json(json_value(tools)))
            source_only = bound_symbol_retry(snapshot) is not None
            context = "" if source_only else self.transport.context_text
            fixed = "\n\n".join(part for part in (prompt, delivery_handoff,
                context if context not in prompt else "",
                coding if coding not in prompt else "", declaration,
                canonical_json(json_value(messages)) if messages else "") if part)
            observations = "" if source_only else task_working_context(
                self.session, self.execution.research_tools.ledger if self.execution.research_tools is not None else None,
                self.execution.research_tools.store if self.execution.research_tools is not None else None,
                self.execution.research_tools.session_id if self.execution.research_tools is not None else "", research_scope=snapshot.research.scope,
                tool_names=snapshot.tool_names, provider=self.provider, fixed_context=fixed)
            # Canonical replay excludes optional memory candidates. Each continuation must
            # carry its frozen context without relying on a warm wire history.
            options = replace(options, supplied=options.supplied | {"tool_call_observations"},
                tool_call_history_groups=1 if source_only else options.tool_call_history_groups,
                tool_call_observations="\n\n".join(part for part in (
                context if context and context not in prompt else "", observations, delivery_handoff,
                coding if coding and coding not in prompt else "") if part))
            if self.session.task_kind == "research" and self.session.policy.strict_research:
                options = replace(options, output_token_limit=4096)
                from codey.research.answer_contract import AnswerScope

                if self.native and isinstance(snapshot.research.scope, AnswerScope):
                    options = replace(options, require_tool_call=True)
                if snapshot.research.scope is not None:
                    options = replace(options, tool_call_history_groups=1)
        if scoped_contract:
            options = replace(options, supplied=options.supplied | {"tool_call_contract"},
                tool_call_contract=scoped_tool_contract)
            if text_result_spans:
                options = replace(options, tool_call_results=text_result_spans)
        if schema_constrained:
            from codey.providers.text_tool_schema import text_tool_schema

            options = replace(options, tool_call_json=True, tool_call_schema=text_tool_schema(list(tools)))
        request: ProviderRequest
        if self.native and messages:
            results = tuple(messages)
            if self.window is not None:
                request = (AcknowledgeToolResultsBoundRequest(results, self.window, tools, options) if acknowledge
                           else SendToolResultsBoundRequest(results, self.window, tools, options))
            else:
                request = (AcknowledgeToolResultsRequest(results, tools, options) if acknowledge
                           else SendToolResultsRequest(results, tools, options))
        else:
            request = SendTurnRequest(prompt, tools, options) if self.native else SendTextRequest(prompt, options)
        prepare_provider_request(self.sink, request, counters=(state.counters, counters or state.counters))

    def prepare_turn(self, state: RuntimeOperationState, *, prefetched: list[ToolResult] | None = None) -> None:
        self.restore_facts()
        turn = state.logical_turn + 1
        if turn > self.session.max_turns:
            self.close(state, KernelResult(False, self.session.last_done_text or "max turns reached", turn - 1, "max_turns"))
            return
        from codey.operations.knowledge_context import knowledge_background

        recalled = knowledge_background(self.execution.research_tools, self.session, str(self.transport.user_task or ""))
        snapshot = capture_turn_snapshot(self.session, native=self.native)
        if (prefetched is None and state.logical_turn == 0 and state.leaf == operation_payload.LEAF_WRITER_RUNNING
                and self.prefetch_required_sources(snapshot)):
            return
        snapshot, handoff = self.request_snapshot(snapshot)
        prepared = prepare_kernel_turn(self.session, user_task=str(self.transport.user_task or ""),
            context_text="\n\n".join(part for part in (self.transport.context_text, recalled) if part),
            native=self.native, task_guidance=self.request.task_guidance,
            completion_context=self.completion_context,
            request_scoped_contract=self.request_scoped_context, snapshot=snapshot)
        prompt = prepared.prompt
        if prefetched:
            results = "\n\n".join(task_result_context(self.session, result, snapshot.research.scope,
                                                     tool_names=snapshot.tool_names)
                                    for result in prefetched)
            prompt += "\n\nRequired source results from the runtime's initial access:\n" + results
        question_raw = self.durable.task(self.sink.run_id).state.get("question")
        if question_raw is not None:
            question = decode_json_object(thaw_json(question_raw))
            if question["state"] == "answered":
                # An answer can arrive before the worker parks, while this
                # transport still predates resume_input. Use the durable fact
                # for both warm continuation and cold recovery.
                answer = require_json_string(question["answer"], "answer")
                prompt += (f"\n\nThe user answered this question: {require_json_string(question['question'], 'question')}\nAnswer: {answer}\n"
                           "Continue the original task within its existing authorization. "
                           "Command execution still requires its own approval.")
        if state.logical_turn and self.boundary_text():
            prompt += "\n\nNew user input at provider boundary:\n" + self.boundary_text()
        admission = admit_context(prompt, self.transport.memory_sources, request_scope=self.transport.context_scope,
            require_all=not self.attempt_gate_bound, admit_hints=self.transport.admit_hints)
        if not self.attempt_gate_bound:
            record_outbound(self.observation.trace_recorder, prompt, admission=admission)
        progress = KernelProgress(self.transport.stagnant_turns, snapshot=state.counters.progress)
        task = str(self.transport.user_task or self.session.task_text or "").strip()
        task_span = (TaskPromptSpan.for_task(self.sink.run_id, task, retry=False)
                     if self.request_scoped_context and self.capabilities.request_scoped_tool_context else None)
        if not self.request_scoped_observations:
            prompt = "\n\n".join(part for part in (prompt, task_working_context(
                self.session, self.execution.research_tools.ledger if self.execution.research_tools is not None else None,
                self.execution.research_tools.store if self.execution.research_tools is not None else None,
                self.execution.research_tools.session_id if self.execution.research_tools is not None else "",
                research_scope=snapshot.research.scope, tool_names=snapshot.tool_names,
                provider=self.provider)) if part)
        self.freeze(state, snapshot, turn=turn, prompt=prompt, handoff=handoff,
                    counters=replace(state.counters, progress=progress.snapshot()),
                    coding_context_prepared=True, initial_source_batch=prefetched is not None, task_span=task_span)

    def prefetch_required_sources(self, snapshot: TurnSnapshot) -> bool:
        from codey.operations.research.acquisition_handoff import initial_source_calls

        calls = initial_source_calls(self.session, snapshot, self.completion_context.research_ledger,
                                     self.execution.browser_session)
        if not calls:
            return False
        names = tuple(dict.fromkeys((*snapshot.tool_names, *(call.name for call in calls))))
        self._execute_calls(calls, replace(snapshot, tool_names=names), turn=0)
        return True

    def interpret(self, state: RuntimeOperationState) -> None:
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
            self.advance(state, operation_payload.LEAF_CLOSING)
            return
        if turn > self.session.max_turns:
            self.close(state, KernelResult(False, self.session.last_done_text or "max turns reached",
                                           self.session.max_turns, "max_turns"))
            return
        kernel_events._emit_turn_event(self.observation.on_event, turn, reply)
        if self.native and not isinstance(reply, AssistantTurn):
            raise RecoveryFailed("native provider did not return a structured turn")
        if isinstance(reply, AssistantTurn) and reply.finish is TurnFinish.OUTPUT_LIMIT:
            counters = state.counters.with_length_continuation(turn)
            if counters is None:
                self.close(state, KernelResult(False, "provider output truncated without a new recovery opportunity",
                                               turn, "provider_failure"))
                return None
            if not reply.tool_calls:
                completion = ("submit_research_answer for the complete task" if snapshot.research.scope is not None else
                              "done(summary) if the verified task is complete")
                self.retry(state, "Your previous response was truncated. No operation from that response was executed. "
                           "Use smaller complete tool calls for the remaining work; do not repeat completed work. "
                           "Submit the next required tool call, or call " + completion + ". "
                           "Do not output ordinary explanation or continue partial tool arguments.",
                           counters=counters)
                return
        decoded = decode_text_turn(reply) if isinstance(reply, str) else reply
        plan = kernel_protocol.normalize_turn(decoded, snapshot=snapshot)
        record_turn(self.observation.trace_recorder, phase=self.session.task_kind, turn=turn,
                    snapshot=snapshot, plan=plan, native=self.native)
        if plan.protocol_error:
            invalid = state.counters.invalid_turns + 1
            threshold = self.transport.stagnant_turns
            prompt = kernel_prompt._repair_prompt(plan, snapshot=snapshot, native=self.native, reply=reply)
            previous_ref = require_json_string(original.get("work_ref", ""), "work_ref")
            previous = (self.decision(replace(state, work_ref=previous_ref))
                        if previous_ref and state.counters.invalid_turns else None)
            repeated = previous is not None and previous.get("purpose") == "retry" and previous.get("prompt") == prompt
            if repeated or threshold is not None and invalid >= max(1, threshold):
                self.close(state, KernelResult(False,
                        f"stopped after {invalid} invalid tool requests"
                        + (" (repeated response without new diagnostic information)" if repeated else "")
                        + f": {plan.protocol_error}", turn, "protocol"),
                    counters=replace(state.counters, invalid_turns=invalid))
                return
            self.retry(state, prompt, error=plan.protocol_error, counters=replace(state.counters, invalid_turns=invalid))
            return
        kind = plan.control.kind if plan.control is not None else ""
        if kind == "ask_user":
            from codey.task.questions import question_args
            question = question_args(dict(plan.control_args))
            identity = f"{self.transport.run_id}:{self.effect_scope}" if self.effect_scope else self.transport.run_id
            question_id = "question_" + hashlib.sha256(f"{identity}:{turn}".encode()).hexdigest()[:24]
            self.advance(state, operation_payload.LEAF_CONTROL_READY, data=QuestionDecision(
                purpose="question", question=question, question_id=question_id,
                result=KernelResult(False, question["question"], turn, "question").to_payload()))
            return
        audit = None
        if plan.syntax_closure is not None:
            ref = self.sink.managed_outputs.write_json_artifact(
                session_id=self.sink.session_id, run_id=self.sink.run_id,
                payload={"schema_version": 1, "kind": "syntax_closed", "source_response_ref": state.response_ref,
                         "candidate": plan.syntax_closure.candidate, "suffix": plan.syntax_closure.suffix,
                         "transport": "native_arguments" if isinstance(reply, AssistantTurn)
                         and "malformed_native_calls" in reply.raw else "text"})
            audit = self.sink.managed_outputs.json_artifact_record(ref, owner_task_id=self.sink.run_id,
                owner_effect_id=None, metadata={"kind": "syntax_closed"})
        self.advance(state, operation_payload.LEAF_COMPLETION_PENDING if kind == "done" else operation_payload.LEAF_PLAN_READY,
                     counters=replace(state.counters, invalid_turns=0), audit=audit)

    def retry(self, state: RuntimeOperationState, prompt: str, *, error: str = "", counters: RuntimeExecutionCounters | None = None) -> None:
        decision = RetryDecision(purpose="retry", prompt=prompt, error=error)
        self.advance(state, operation_payload.LEAF_CONTROL_READY, data=decision, counters=counters or state.counters)

    def plan(self, state: RuntimeOperationState) -> ToolPlan:
        received = self.response(state)
        reply = decode_text_turn(received.reply) if isinstance(received.reply, str) else received.reply
        plan = kernel_protocol.normalize_turn(reply, snapshot=received.snapshot)
        if plan.protocol_error:
            raise RecoveryFailed("accepted plan no longer parses under its captured contract")
        return plan

    def execute_tools(self, state: RuntimeOperationState) -> KernelResult | None:
        self.restore_facts()
        received, plan = self.response(state), self.plan(state)
        calls, turn = list(plan.calls), received.logical_turn
        if not received.snapshot.implementation_compatible:
            raise RecoveryFailed("captured tool implementation is unavailable or incompatible")
        if state.leaf == operation_payload.LEAF_PLAN_READY:
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
        if state.leaf == operation_payload.LEAF_PLAN_READY:
            query = self._conflict_followup(received.snapshot, calls, turn)
            if query is not None:
                # Acquire before any submission freezes the current evidence.
                calls.insert(0, query)
        elif calls and all(call.name != "web_search" for call in calls):
            # A settled host search occupies the original first batch slot.
            # Recovery must preserve that slot when resuming its submission.
            scope = f"{self.transport.run_id}:{self.effect_scope}" if self.effect_scope else self.transport.run_id
            first = self.session._memory_results.get(turn_effect_id(scope, turn, 0))
            if first is not None and _host_acquisition_result(first):
                calls.insert(0, first.call)
        self._execute_calls(calls, received.snapshot, turn=turn)
        return None

    def _conflict_followup(self, snapshot: TurnSnapshot, calls: list[ToolCall], turn: int) -> ToolCall | None:
        from codey.operations.research.acquisition_handoff import conflict_followup

        return conflict_followup(self.session, self.execution.research_tools, snapshot, calls, turn)

    def _execute_calls(self, calls: list[ToolCall], snapshot: TurnSnapshot, *, turn: int) -> None:
        kernel_events._emit_tool_starts(self.observation.on_event, self.session, turn, calls)
        result = wiring._call_execute_turn(self.session, calls, self.execution.executors, self.transport.run_id,
            self.effect_scope, turn, self.execution.project_path, self.execution.tool_fns,
            self.execution.research_tools, self.execution.change_tracker, self.sink.managed_outputs,
            self.sink.session_id, self.execution.permission_profile, self.session._memory_results,
            self.sink, snapshot.allowed, wiring._outer_evidence_for_context(self.completion_context),
            self.execution.workspace_ignored_paths, self.execution.workspace_revision_store,
            turn, True, self.transport.stop_flag, durable=self.durable, snapshot=snapshot,
            on_event=self.observation.on_event, browser_session=self.execution.browser_session)
        if isinstance(result, KernelResult):
            raise RecoveryFailed(result.summary)

    def provider_delivery(self, results: list[ToolResult], snapshot: TurnSnapshot, *,
                          boundary_prompt: str = "") -> tuple[list[ToolResult], str]:
        """Host searches are observations, never replies to invented native call IDs."""
        if not self.native:
            return results, boundary_prompt
        supplemental = "\n\n".join(task_result_context(self.session, row, snapshot.research.scope,
            tool_names=snapshot.tool_names) for row in results if _host_acquisition_result(row))
        return ([row for row in results if not _host_acquisition_result(row)],
                "\n\n".join(part for part in (boundary_prompt, supplemental) if part))

    def observe_progress(self, view: SessionView, action: RuntimeAction) -> None:
        results = self.results(view, action.delivery_batch_id)
        state = view.state
        if state is None:
            raise RecoveryFailed("progress observation requires operation state")
        self.session.turn = state.turn
        previous = state.counters.progress
        progress = KernelProgress(self.transport.stagnant_turns, snapshot=previous)
        facts, cycle = self._project_check_observations(results)
        progress.observe(results, self.session, batch_id=action.delivery_batch_id,
                         check_facts=facts, check_cycle=cycle.key if cycle is not None else "")
        self.sink.delivery_batch_id = action.delivery_batch_id
        self.sink.record_progress(previous, progress.snapshot())

    def _project_check_observations(
        self, results: Sequence[ToolResult],
    ) -> tuple[dict[int, ProjectCheckComparison], ProjectCheckCycle | None]:
        if not self.session.project:
            return {}, None

        def compare(result: ToolResult) -> ProjectCheckComparison | None:
            fact = self._compare_project_check(result)
            return fact if fact is not None and fact.complete else None

        facts = {index: fact for index, result in enumerate(results) if (fact := compare(result)) is not None}
        cycle = project_check_cycle(results, self.session._memory_results, compare) if facts else None
        return facts, cycle

    def _compare_project_check(self, result: ToolResult, *,
                               include_accepted: bool = False) -> ProjectCheckComparison | None:
        from codey.operations.progress_comparison import compare_project_check
        from codey.operations.tool_tasks.reads import settled_check_for_progress

        execution = settled_check_for_progress(self.durable, result, owner_task_id=self.sink.run_id,
                                                receipts=self.session._memory_results, include_accepted=include_accepted)
        if execution is None:
            return None
        ref = execution.execution_ref
        if ref not in self.project_check_comparisons:
            self.project_check_comparisons[ref] = compare_project_check(
                execution, session=self.session, store=self.sink.managed_outputs, session_id=self.sink.session_id)
        return self.project_check_comparisons[ref]

    def _request_check_handoff(self, snapshot: TurnSnapshot, handoff: str | None) -> tuple[TurnSnapshot, str]:
        from codey.operations.project.project_check_handoff import project_check_handoff

        if handoff is None:
            snapshot, handoff = self.request_snapshot(snapshot)
        project = self.execution.project_path
        if project is None:
            return snapshot, handoff
        evidence = project_check_handoff(self.session, snapshot, project,
                                         lambda result: self._compare_project_check(result, include_accepted=True))
        return snapshot, "\n\n".join(part for part in (handoff, evidence) if part)

    def deliver_results(self, view: SessionView, action: RuntimeAction) -> None:
        state = view.state
        if state is None:
            raise RecoveryFailed("result delivery requires operation state")
        results = self.results(view, action.delivery_batch_id)
        self.sink.delivery_batch_id = action.delivery_batch_id
        turn = state.turn
        kernel_events._emit_tool_results(self.observation.on_event, self.session, results,
            run_id=f"{self.transport.run_id}:{self.effect_scope}" if self.effect_scope else self.transport.run_id,
            turn=turn)
        if turn == 0 and results and all(result.call.name in {"open_url", "browser_navigate", "codemode"}
                and result.call.call_id.startswith("required-source:") for result in results) and not self.cancelled():
            self.prepare_turn(state, prefetched=results)
            return
        from codey.operations.research.research_completion_checks import report_completes_task
        from codey.research.answer_contract import submitted_answer

        if (report_completes_task(self.session) and not self.cancelled() and results and results[-1].ok
                and results[-1].call.name == "submit_research_answer"
                and all(_host_acquisition_result(row) for row in results[:-1])
                and submitted_answer(self.session) is not None):
            self.advance(state, operation_payload.LEAF_COMPLETION_PENDING)
            return
        progress = KernelProgress(self.transport.stagnant_turns, snapshot=state.counters.progress)
        cancelled = self.cancelled()
        if cancelled or turn >= self.session.max_turns or progress.stopped:
            reason = "stopped" if cancelled else "max_turns" if turn >= self.session.max_turns else "no_progress"
            summary = {"stopped": "stopped", "no_progress": "stopped after repeated tool results without progress",
                       "max_turns": self.session.last_done_text or "max turns reached"}[reason]
            from codey.operations.kernel_control import no_progress_result
            self.close(state, no_progress_result(self.session, turn) if reason == "no_progress"
                       else KernelResult(False, summary, turn, reason))
            return
        snapshot, handoff = self.request_snapshot(capture_turn_snapshot(self.session, native=self.native),
                                                 delivered_results=results)

        def result_context(result: ToolResult) -> str:
            value: str = task_result_context(self.session, result, snapshot.research.scope, tool_names=snapshot.tool_names)
            return value

        transport_results, boundary_prompt = self.provider_delivery(results, snapshot, boundary_prompt=self.boundary_text())
        advance = wiring._advance_after_results(self.native, transport_results, self.session, None,
            completion_context=self.completion_context, boundary_prompt=boundary_prompt,
            result_context=result_context)
        messages, prompt, spans = advance.messages, advance.prompt, advance.spans
        observations = "" if self.request_scoped_observations else task_working_context(
            self.session, self.execution.research_tools.ledger if self.execution.research_tools is not None else None,
                self.execution.research_tools.store if self.execution.research_tools is not None else None,
                self.execution.research_tools.session_id if self.execution.research_tools is not None else "", research_scope=snapshot.research.scope,
                tool_names=snapshot.tool_names, provider=self.provider)
        reminder = progress.repeat_feedback() if progress.warning_batch_id != action.delivery_batch_id else ""
        if reminder:
            from codey.operations.browser.browser_task_handoff import observation_recovery_handoff
            from codey.operations.progress_comparison import progress_comparison

            if any(progress_comparison(row, self.session._memory_results).browser_observation for row in results):
                browser = self.execution.browser_session
                context = replace(self.completion_context, browser_document=browser.document_identity() if browser else None)
                snapshot, recovery = observation_recovery_handoff(self.session, snapshot, context)
                handoff = "\n\n".join(part for part in (handoff, recovery) if part)
        if self.native and messages:
            messages[-1] = replace(messages[-1], user_content="\n\n".join(
                part for part in (observations, messages[-1].user_content, reminder) if part))
        else:
            # Keep the latest result after historical observations: previous
            # findings must not displace the passage currently being inspected.
            prompt = "\n\n".join(part for part in (observations, prompt) if part)
            shift = len(observations) + 2 if observations else 0
            spans = tuple(replace(span, start=span.start + shift, end=span.end + shift) for span in spans)
            if reminder:
                prompt += "\n\n" + reminder
        self.freeze(state, snapshot, turn=turn + 1, prompt=prompt, messages=messages, text_result_spans=spans,
                    handoff=handoff)

    def boundary_text(self) -> str:
        task = self.durable.task(self.sink.run_id)
        receipt_raw = task.state.get("generation_boundary_receipt")
        receipt: Mapping[str, object] = receipt_raw if isinstance(receipt_raw, Mapping) else dict[str, object]()
        submission_ids = receipt.get("submission_ids", ())
        ids = submission_ids if isinstance(submission_ids, (list, tuple)) else ()
        texts: list[str] = []
        for identity in ids:
            if not isinstance(identity, str):
                continue
            content = self.durable.submission(identity).content
            if isinstance(content, Mapping):
                texts.append(str(content.get("text", "")))
        return "\n\n".join(texts)

    def boundary(self, action: RuntimeAction) -> None:
        from codey.operations.generation_boundary import GenerationBoundaryRequest
        if self.observation.on_boundary is None:
            raise RecoveryFailed("an admitted provider generation requires a durable boundary callback")
        self.observation.on_boundary(GenerationBoundaryRequest(action.reason))

    def close(self, state: RuntimeOperationState, result: KernelResult, *, counters: RuntimeExecutionCounters | None = None) -> None:
        from codey.operations.explanation_repair import active

        if result.stop_reason == "max_turns" and not result.completed:
            draft = result.summary if result.summary != "max turns reached" else ""
            result = replace(result, summary="max turns reached; the task is incomplete."
                + ("\n\nUnaccepted draft:\n" + draft if draft else ""))
        if active((counters or state.counters).progress) and result.stop_reason in {"no_progress", "max_turns", "protocol"}:
            result = replace(result, stop_reason="answer_repair_exhausted")
        self.advance(state, operation_payload.LEAF_CLOSING, data=ClosingDecision(purpose="closing", result=result.to_payload()),
                     counters=counters)
