"""Durable effect receipts for the shared task turn kernel."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from codey.durable.model import ArtifactRecord
    from codey.durable.session import DurableSession

from codey.providers.base import AssistantTurn
from codey.runtime.core.models import ToolCall
from codey.runtime.core.operation_payload import RuntimeExecutionCounters
from codey.runtime.core.progress import ProgressSnapshot
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    SENT_STATE_SETTLED,
    SETTLEMENT_STATUS_ERROR,
    SETTLEMENT_STATUS_OK,
    RuntimeEffectError,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    RuntimeEffectStore,
    compute_args_digest,
)
from codey.runtime.effects.replay_policy import ReplayClass, tool_replay_policy
from codey.runtime.effects.safe_tool_replay import replay_args_for_tool_call
from codey.runtime.effects.tool_result_delivery import (
    DeliveryBatchIntent,
    DeliveryBatchItem,
    compute_batch_digest,
    new_batch_id,
)
from codey.toolchain.tool_spec import spec_for_tool


def _runtime_name(name: str) -> str:
    return {"list_dir": "ls", "read_file": "read", "grep": "search",
            "find_references": "references"}.get(name, name)


def admitted_durable(state: Any, task_id: str) -> DurableSession | None:
    """Return durable state only for an actually admitted task.

    Some kernel entry points are exercised as projection-only operations (for
    example a recovery delivery test) and intentionally have no durable task.
    They may still share an ``AppContext`` with a durable session, so checking
    task admission keeps those paths from manufacturing an effect against a
    nonexistent task while preserving the durable boundary for real runs.
    """
    if state is None:
        return None
    durable = getattr(state, "durable", None)
    if durable is None:
        return None
    lookup = durable.task
    if durable is None or not callable(lookup) or not task_id:
        return None
    try:
        lookup(task_id)
    except KeyError:
        return None
    return cast("DurableSession", durable)


class KernelEffectSink:
    """Commit one turn's tool intents before any tool can run."""

    def __init__(self, mutations: Any, *, session_id: str, run_id: str,
                 provider_id: str, phase: str = "writer",
                 recovered_batch_id: str = "",
                 managed_outputs: Any = None,
                 durable: Any = None,
                 parent_effect_id: str = "") -> None:
        self.mutations = mutations
        self.session_id = session_id
        self.run_id = run_id
        self.provider_id = provider_id
        self.phase = phase
        self.delivery_batch_id = recovered_batch_id
        self.managed_outputs = managed_outputs
        self.durable = durable
        self.parent_effect_id = parent_effect_id
        self._request_context: dict[str, object] = {}
        store = RuntimeEffectStore(durable=durable)
        previous = store.load_effects(session_id, run_id)
        self._replay_classes = {row.intent.effect_id: row.intent.replay_class for row in previous}
        self._old_pending = {row.intent.effect_id for row in previous if row.is_pending}

    def for_child(self, call: ToolCall, *, turn: int) -> KernelEffectSink:
        """Bind nested effects to the existing parent; only it owns delivery."""
        matches = [effect for effect in self.durable.effects(self.run_id)
                   if effect.category == EFFECT_CATEGORY_TOOL_CALL
                   and effect.intent.get("tool_name") == "codemode"
                   and effect.intent.get("call_id", "") == call.call_id
                   and effect.intent.get("turn") == turn
                   and effect.args_digest == compute_args_digest(call.args)]
        if len(matches) != 1:
            raise ValueError("CodeMode requires one original parent effect")
        return KernelEffectSink(self.mutations, session_id=self.session_id, run_id=self.run_id,
            provider_id=self.provider_id, phase=self.phase, managed_outputs=self.managed_outputs,
            durable=self.durable, parent_effect_id=matches[0].effect_id)

    def begin_turn(
        self,
        items: list[tuple[str, ToolCall, int]],
        *,
        turn: int,
        specs: Any = None,
        scope: str = "",
    ) -> None:
        intents: list[RuntimeEffectIntent] = []
        batch_items: list[DeliveryBatchItem] = []
        batch_key = f"{self.run_id}:{scope}" if scope else self.run_id
        batch_id = new_batch_id(batch_key, turn)
        for identity, call, index in items:
            if identity in self._old_pending:
                continue
            name = _runtime_name(call.name or "")
            spec = specs.get(call.name) if specs is not None else spec_for_tool(call.name or "")
            replay_class = (spec.replay_class if spec is not None
                            else tool_replay_policy(name).replay_class)
            replay_args = replay_args_for_tool_call(ToolCall(name, dict(call.args or {})))
            self._replay_classes[identity] = replay_class
            intents.append(RuntimeEffectIntent(
                effect_id=identity, effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id, run_id=self.run_id,
                phase=self.phase, turn=turn, tool_index=index,
                tool_name=name, tool_id=f"{turn}:{index}",
                call_id=str(getattr(call, "call_id", "") or ""),
                args_digest=compute_args_digest(call.args), replay_class=replay_class,
                delivery_batch_id="" if self.parent_effect_id else batch_id,
                parent_effect_id=self.parent_effect_id,
                replay_args=replay_args,
            ))
            batch_items.append(DeliveryBatchItem(
                tool_index=index, tool_name=name, ref=identity,
                replay_class=replay_class,
            ))
        if not batch_items:
            return
        batch_tuple = tuple(batch_items)
        self.mutations.begin_tool_batch(
            self.session_id, self.run_id, intents=tuple(intents),
            delivery_intent=None if self.parent_effect_id else DeliveryBatchIntent(
                batch_id=batch_id, session_id=self.session_id, run_id=self.run_id,
                turn=turn, items=batch_tuple,
                batch_digest=compute_batch_digest(batch_tuple),
            ),
        )
        if not self.parent_effect_id:
            self.delivery_batch_id = batch_id

    def has_unsettled(self, identity: str) -> bool:
        return identity in self._old_pending

    def bind_request(self, snapshot: Any, *, logical_turn: int, window_ref: Any = None) -> None:
        from codey.operations.provider_receipts import snapshot_description

        self._request_context = {"logical_turn": logical_turn, "snapshot": snapshot_description(snapshot),
                                 "window": window_ref.to_payload() if window_ref is not None else None,
                                 "effect_scope": "", "close_reply": False}

    def counters(self) -> RuntimeExecutionCounters:
        from codey.durable.checkpoint import operation_from_checkpoint

        state = operation_from_checkpoint(self.durable.task(self.run_id), self.session_id)
        if state is None:
            raise RuntimeEffectError("execution counters require an admitted operation")
        return state.counters

    def record_progress(self, expected: ProgressSnapshot, observed: ProgressSnapshot) -> None:
        self.mutations.record_progress(self.session_id, self.run_id, batch_id=self.delivery_batch_id,
                                       expected=expected, observed=observed)

    def record_response(self, effect_id: str, reply: Any) -> None:
        from codey.operations.provider_receipts import RESPONSE_KIND, write_provider_artifact
        from codey.providers.response_record import encode_reply

        if self.managed_outputs is None:
            raise RuntimeEffectError("provider responses require managed output storage")
        artifact = write_provider_artifact(self.managed_outputs, session_id=self.session_id, run_id=self.run_id,
            effect_id=effect_id, kind=RESPONSE_KIND, payload=encode_reply(reply))
        self.mutations.record_provider_response(self.session_id, self.run_id, effect_id=effect_id, artifact=artifact,
            messages=(("tool_call", {"response_ref": artifact.handle}),)
                if isinstance(reply, AssistantTurn) and reply.tool_calls else ())

    def load_response(self, session: Any, response_ref: str, *, window_ref: Any = None) -> Any:
        from codey.operations.kernel_protocol import ReceivedProviderTurn
        from codey.operations.provider_receipts import (
            REQUEST_KIND,
            RESPONSE_KIND,
            read_provider_artifact,
            request_artifact_for_effect,
            restore_snapshot,
        )
        from codey.providers.response_record import decode_reply
        from codey.runtime.core.protocol_window import ProtocolWindowRef

        artifact = self.durable.artifact(response_ref)
        effect_id = artifact.owner_effect_id
        effect = next((row for row in RuntimeEffectStore(durable=self.durable).load_effects(self.session_id, self.run_id)
                       if row.intent.effect_id == effect_id), None)
        if effect is None or effect.intent.effect_category != EFFECT_CATEGORY_PROVIDER_SEND:
            raise RuntimeEffectError("response has no original provider intent")
        request = read_provider_artifact(self.managed_outputs, request_artifact_for_effect(self.durable, effect),
            kind=REQUEST_KIND, session_id=self.session_id, run_id=self.run_id, effect_id=effect_id)
        response = read_provider_artifact(self.managed_outputs, artifact, kind=RESPONSE_KIND,
            session_id=self.session_id, run_id=self.run_id, effect_id=effect_id)
        if not isinstance(request, dict) or type(request.get("logical_turn")) is not int or request["logical_turn"] < 1:
            raise RuntimeEffectError("response has no original logical turn")
        if not isinstance(request.get("effect_scope"), str):
            raise RuntimeEffectError("response has no frozen effect scope")
        snapshot = restore_snapshot(request.get("snapshot"))
        window = ProtocolWindowRef.from_payload(request["window"]) if request.get("window") is not None else None
        if snapshot.policy != session.policy or window != window_ref:
            raise RuntimeEffectError("response authorization or protocol window changed")
        args = request.get("args")
        prompt = args[0] if isinstance(args, list) and args and isinstance(args[0], str) else ""
        return ReceivedProviderTurn(decode_reply(response), snapshot, prompt, owner_session=session,
                                    logical_turn=request["logical_turn"], window_ref=window, effect_scope=request["effect_scope"])

    def pending_provider_settlement(self) -> tuple[RuntimeEffectSettlement, tuple[tuple[str, dict[str, object]], ...]] | None:
        """Derive the open boundary from durable response facts, including after restart."""
        from codey.operations.provider_receipts import RESPONSE_KIND, read_provider_artifact
        from codey.providers.response_record import decode_reply

        pending = [effect for effect in self.durable.effects(self.run_id)
                   if effect.category == EFFECT_CATEGORY_PROVIDER_SEND and effect.settlement is None]
        if len(pending) > 1:
            raise RuntimeEffectError("multiple provider generations are pending")
        if not pending:
            return None
        effect = pending[0]
        responses = [artifact for artifact in self.durable.artifacts_for_effect(effect.effect_id)
                     if artifact.metadata.get("kind") == RESPONSE_KIND]
        if not responses:
            return None
        if len(responses) != 1:
            raise RuntimeEffectError("provider generation has conflicting responses")
        decode_reply(read_provider_artifact(self.managed_outputs, responses[0], kind=RESPONSE_KIND,
            session_id=self.session_id, run_id=self.run_id, effect_id=effect.effect_id))
        return RuntimeEffectSettlement(effect_id=effect.effect_id, effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
            session_id=self.session_id, run_id=self.run_id, status=SETTLEMENT_STATUS_OK,
            sent_state=SENT_STATE_SETTLED), ()

    def settle(self, identity: str, ok: bool, *, result: Any = None, exit_code: int | None = None) -> None:
        from codey.operations.kernel_receipts import result_receipt_fields, tool_history_messages

        if result is None:
            raise ValueError("tool settlement requires the original result receipt")
        audit = getattr(result, "audit", {})
        fields = result_receipt_fields(
            result, store=self.managed_outputs, session_id=self.session_id,
            run_id=self.run_id, effect_id=identity,
        )
        resolved_exit: int | None = None
        if isinstance(exit_code, int) and type(exit_code) is int:
            resolved_exit = exit_code
        elif isinstance(audit, dict) and type(audit.get("exit_code")) is int:
            resolved_exit = audit.get("exit_code")
        settlement = RuntimeEffectSettlement(
            effect_id=identity, effect_category=EFFECT_CATEGORY_TOOL_CALL,
            session_id=self.session_id, run_id=self.run_id,
            status=SETTLEMENT_STATUS_OK if ok else SETTLEMENT_STATUS_ERROR,
            error_code="" if ok else "tool_error",
            replay_class=self._replay_classes.get(identity, ReplayClass.UNSAFE),
            **fields,
            exit_code=resolved_exit,
            replay_count=1 if identity in self._old_pending else 0,
            replayed_from_effect_id=identity if identity in self._old_pending else "",
        )
        artifacts: tuple[ArtifactRecord, ...] = ()
        managed = audit.get("managed_output") if isinstance(audit, dict) else None
        if isinstance(managed, dict) and self.managed_outputs is not None and self.durable is not None:
            artifacts = (self.managed_outputs.durable_record(
                str(managed.get("handle") or ""), session_id=self.session_id, run_id=self.run_id,
                owner_task_id=self.run_id, owner_effect_id=identity,
            ),)
        self.mutations.settle_tool_effect(
            self.session_id, self.run_id,
            settlement,
            messages=tool_history_messages(settlement) if self.durable is not None and not self.parent_effect_id else (),
            artifacts=artifacts,
        )

__all__ = ["KernelEffectSink", "admitted_durable"]
