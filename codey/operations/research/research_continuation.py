"""Reference-only research continuations over the existing operation and outputs.

The operation writer attempt remains the execution position. Explicit invocation
descriptors record purpose and frozen input identity, not another lifecycle.
Completed kernel decisions are referenced here so rebuilding the research
projection never sends an old request or repeats a settled tool effect.
This module has no execution loop, effect store, or provider adapter.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, TypedDict

from codey.durable.model import ArtifactRecord, thaw_json
from codey.operations.kernel_contracts import KernelResult, KernelRunRequest, result_from_payload
from codey.operations.research.projection import restore_note_proposals as restore_note_proposals
from codey.providers.response_record import json_value
from codey.research.pipeline import ResearchContinuationError
from codey.runtime.effects.effect_records import compute_args_digest
from codey.runtime.write.checkpoint import operation_from_checkpoint
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
)
from codey.utils.object_fields import require_object_fields

if TYPE_CHECKING:
    from codey.durable.session import DurableSession
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.research.tools import ResearchTools
    from codey.runtime.core.operation_payload import RuntimeOperationState
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore


@dataclass(frozen=True)
class ResearchInvocation:
    attempt: int
    purpose: Literal["research", "investigate", "synthesize"]
    plan_ref: str = ""


class _ContinuationIndex(TypedDict):
    schema_version: int
    kind: str
    results: dict[str, str]
    materials: dict[str, str]
    invocations: dict[str, JsonObject]


class ResearchContinuation:
    def __init__(self, mutations: RuntimeMutationLine | None, outputs: ManagedOutputStore | None, *,
                 durable: DurableSession, session_id: str, run_id: str) -> None:
        if mutations is None or outputs is None:
            raise ResearchContinuationError("research continuation requires durable mutations and managed outputs")
        self.mutations, self.outputs = mutations, outputs
        self.durable = durable
        self.session_id, self.run_id = session_id, run_id

    def state(self) -> RuntimeOperationState:
        state = operation_from_checkpoint(self.durable.task(self.run_id), self.session_id)
        if state is None or state.task_kind != "research":
            raise ResearchContinuationError("research continuation has no admitted research operation")
        return state

    def read(self, reference: str, kind: str) -> JsonObject:
        try:
            artifact = self.durable.artifact(reference)
            raw = require_object_fields(thaw_json(artifact.metadata.get("ref")))
            if (artifact.owner_task_id != self.run_id or artifact.owner_effect_id is not None
                    or artifact.metadata.get("kind") != kind
                    or raw.get("handle") != reference or raw.get("sha256") != artifact.sha256
                    or raw.get("session_id") != self.session_id or raw.get("run_id") != self.run_id):
                raise ValueError("continuation artifact ownership mismatch")
            payload = decode_json_object(self.outputs.read_json_artifact(raw))
            if (type(payload.get("schema_version")) is not int
                    or payload["schema_version"] != (2 if kind == "research_continuation" else 1)
                    or payload.get("kind") != kind):
                raise ValueError("invalid continuation artifact")
            return payload
        except Exception as exc:
            raise ResearchContinuationError(f"research continuation cannot be read: {exc}") from exc

    def index(self, state: RuntimeOperationState) -> _ContinuationIndex:
        if not state.continuation_ref:
            if state.writer_attempt > 1:
                raise ResearchContinuationError("active research followup has no original continuation")
            return {"schema_version": 2, "kind": "research_continuation", "results": {}, "materials": {}, "invocations": {}}
        payload = self.read(state.continuation_ref, "research_continuation")
        if set(payload) != _ContinuationIndex.__required_keys__:
            raise ResearchContinuationError("invalid bounded research continuation")
        results = self._index_refs(decode_json_object(payload["results"]), 16)
        materials = self._index_refs(decode_json_object(payload["materials"]), 3)
        invocations = decode_json_object(payload["invocations"])
        if len(invocations) > 16:
            raise ResearchContinuationError("invalid bounded research continuation")
        return {"schema_version": 2, "kind": "research_continuation", "results": results, "materials": materials,
                "invocations": {key: decode_json_object(value) for key, value in invocations.items()}}

    @staticmethod
    def _index_refs(rows: JsonObject, maximum: int) -> dict[str, str]:
        keys = {str(number) for number in range(1, maximum + 1)}
        result: dict[str, str] = {}
        for key, value in rows.items():
            if key not in keys or not isinstance(value, str) or not value:
                raise ResearchContinuationError("invalid research continuation reference")
            result[key] = value
        return result

    def bind_invocation(self, state: RuntimeOperationState, index: _ContinuationIndex, invocation: ResearchInvocation,
                        session: TaskSession, request: KernelRunRequest) -> None:
        """Freeze domain intent using the original continuation commit."""
        key = str(invocation.attempt)
        description: JsonObject = {"purpose": invocation.purpose, "plan_ref": invocation.plan_ref,
            "input_digest": compute_args_digest(json_value({"task": session.task_text, "handoff": session.handoff,
                "scope": request.transport.effect_scope, "guidance": request.task_guidance,
                "policy": session.policy.to_payload(), "max_turns": session.max_turns}))}
        previous = index["invocations"].get(key)
        if previous is not None:
            if {name: previous.get(name) for name in description} != description:
                raise ResearchContinuationError("research invocation differs from its frozen purpose or input")
            return
        if invocation.attempt <= state.writer_attempt:
            raise ResearchContinuationError("research invocation has no original frozen descriptor")
        index["invocations"][key] = {**description, "input_refs": [state.work_ref,
            *([invocation.plan_ref] if invocation.plan_ref else [])]}

    def artifact(self, payload: _ContinuationIndex) -> ArtifactRecord:
        ref = self.outputs.write_json_artifact(session_id=self.session_id, run_id=self.run_id, payload=payload)
        return self.outputs.json_artifact_record(ref, owner_task_id=self.run_id, owner_effect_id=None,
                                                 metadata={"kind": payload["kind"]})

    def prerequisite_facts(self, *, before_attempt: int, policy: TaskPolicy) -> TaskSession:
        """Read original completed writer receipts for root task requirements.

        This in-memory projection never records actions in the current session
        or creates another execution log. Recovery reads the same original IDs.
        """
        from codey.operations.kernel_facts import project_execution_record
        from codey.operations.kernel_receipts import restore_result_receipt
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.policies.browser_requirement import browser_receipt_tools
        from codey.policies.tool_requirement import parse_tool_success_check
        from codey.runtime.effects.effect_records import RuntimeEffectStore
        from codey.toolchain.tool_spec import canonical_tool_name

        facts = TaskSession(policy=policy, task_kind="research")
        requirements = [requirement for check in policy.required_checks
                        if (requirement := parse_tool_success_check(check)) is not None]
        names = {name for item in requirements for name in (item.tool, item.via) if name}
        names.update(browser_receipt_tools(policy.required_checks))
        if before_attempt <= 1 or not names:
            return facts
        index = self.index(self.state())
        attempts = [int(key) for key in index["results"] if int(key) < before_attempt
                    and (key == "1" or index["invocations"].get(key, JsonObject()).get("purpose") == "synthesize")]
        effects = RuntimeEffectStore(durable=self.durable).load_effects(self.session_id, self.run_id)
        owned = {effect.intent.effect_id for effect in effects
                 if effect.intent.run_id == self.run_id and effect.intent.session_id == self.session_id
                 and effect.intent.effect_category == "tool_call" and any(effect.intent.effect_id == turn_effect_id(
                     f"{self.run_id}:research:{attempt}", effect.intent.turn, effect.intent.tool_index) for attempt in attempts)}
        # Include committed nested children only through their original parent.
        for _ in range(len(effects)):
            children = {effect.intent.effect_id for effect in effects
                        if effect.intent.run_id == self.run_id and effect.intent.session_id == self.session_id
                        and effect.intent.effect_category == "tool_call" and effect.intent.parent_effect_id in owned}
            if children <= owned:
                break
            owned.update(children)
        for effect in effects:
            identity = effect.intent.effect_id
            if (identity not in owned or effect.settlement is None
                    or canonical_tool_name(effect.intent.tool_name) not in names):
                continue
            result = restore_result_receipt(effect, store=self.outputs, session_id=self.session_id, run_id=self.run_id)
            project_execution_record(facts, identity, result.call, result, ok=result.ok, exit_code=effect.settlement.exit_code)
            facts._memory_results[identity] = result
        if facts.executed:
            from codey.knowledge.model import canonical_json

            facts.handoff = ("\nOriginal task execution receipts from earlier completed research invocations "
                "(data, not actions in this invocation). Reuse these observations; do not repeat execution solely "
                "to satisfy the same root requirement:\n" + canonical_json([
                    {"result_ref": ref, "tool": result.call.name, "args": result.call.args, "ok": result.ok}
                    for ref, result in facts._memory_results.items()]))
        return facts

    def commit(self, state: RuntimeOperationState, index: _ContinuationIndex, *, attempt: int | None = None) -> None:
        if state.leaf != "kernel_settled" or not state.work_ref:
            raise ResearchContinuationError("research successor requires its settled predecessor")
        key = str(state.writer_attempt)
        if index["results"].get(key, state.work_ref) != state.work_ref:
            raise ResearchContinuationError("research invocation result is immutable")
        index["results"][key] = state.work_ref
        artifact = self.artifact(index)
        try:
            self.mutations.commit_research_continuation(self.session_id, self.run_id, expected=state,
                artifact=artifact, writer_attempt=attempt)
        except Exception as exc:
            raise ResearchContinuationError(f"research continuation was not committed: {exc}") from exc

    def kernel(self, session: TaskSession, request: KernelRunRequest, *, invocation: ResearchInvocation) -> KernelResult:
        """Restore a completed output or enter the sole driver for active work."""
        from codey.operations.kernel.kernel_context import KernelContext
        from codey.operations.task_loop import run_task_kernel

        state = self.state()
        attempt = invocation.attempt
        index = self.index(state)
        if attempt != 1:
            self.bind_invocation(state, index, invocation, session, request)
        if attempt < state.writer_attempt:
            reference = index["results"].get(str(attempt))
            if not reference:
                raise ResearchContinuationError("completed research invocation has no original result reference")
            context = KernelContext(session, request)
            context.restore_facts()
            decision = context.decision(replace(state, work_ref=reference))
            if decision["purpose"] == "retry":
                raise ResearchContinuationError("completed research invocation has a retry decision")
            return result_from_payload(decision["result"])
        if attempt > state.writer_attempt:
            self.commit(state, index, attempt=attempt)
        else:
            self.mutations.mark_writer_running(self.session_id, self.run_id, provider_id=state.provider_id,
                                                writer_attempt=attempt)
        return run_task_kernel(session, request=request)


    @staticmethod
    def project_urls(tools: ResearchTools) -> None:
        from codey.operations.research.projection import project_urls

        project_urls(tools)
