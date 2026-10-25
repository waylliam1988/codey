"""Durable effect receipts for the shared task turn kernel."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from codey.durable.model import ArtifactRecord
    from codey.durable.session import DurableSession

from codey.operations.provider_session import ProviderAdapter
from codey.providers.base import AssistantTurn
from codey.runtime.core.models import ToolCall
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    SENT_STATE_MAYBE_SENT,
    SENT_STATE_NOT_SENT,
    SENT_STATE_SETTLED,
    SETTLEMENT_STATUS_ERROR,
    SETTLEMENT_STATUS_OK,
    RuntimeEffectError,
    RuntimeEffectIntent,
    RuntimeEffectSettlement,
    RuntimeEffectStore,
    compute_args_digest,
    new_effect_id,
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
                 parent_effect_id: str = "",
                 defer_generation_settlement: bool = False) -> None:
        self.mutations = mutations
        self.session_id = session_id
        self.run_id = run_id
        self.provider_id = provider_id
        self.phase = phase
        self.delivery_batch_id = recovered_batch_id
        self.send_index = 0
        self.managed_outputs = managed_outputs
        self.durable = durable
        self.parent_effect_id = parent_effect_id
        self.defer_generation_settlement = defer_generation_settlement
        self._pending_provider_settlement: tuple[RuntimeEffectSettlement, tuple[tuple[str, dict[str, object]], ...]] | None = None
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

    def defer_provider_settlement(
        self,
        settlement: RuntimeEffectSettlement,
        messages: tuple[tuple[str, dict[str, object]], ...] = (),
    ) -> None:
        if not self.defer_generation_settlement:
            raise RuntimeEffectError("provider settlement deferral is disabled")
        if self._pending_provider_settlement is not None:
            raise RuntimeEffectError("a provider generation settlement is already pending")
        self._pending_provider_settlement = (settlement, messages)

    def pending_provider_settlement(self) -> tuple[RuntimeEffectSettlement, tuple[tuple[str, dict[str, object]], ...]] | None:
        return self._pending_provider_settlement

    def clear_provider_settlement(self) -> None:
        self._pending_provider_settlement = None

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

class KernelRecordedProvider(ProviderAdapter):
    """Record provider sends and link result delivery to the preceding tool batch."""

    def __init__(self, provider: Any, sink: KernelEffectSink) -> None:
        super().__init__(provider)
        self.sink = sink

    def _send(self, name: str, *args: Any, **kwargs: Any) -> Any:
        sink = self.sink
        pending = sink.pending_provider_settlement()
        if pending is not None:
            previous, previous_messages = pending
            sink.mutations.settle_provider_effect(
                sink.session_id, sink.run_id, previous, messages=previous_messages,
            )
            sink.clear_provider_settlement()
        sink.send_index += 1
        effect_id = new_effect_id(EFFECT_CATEGORY_PROVIDER_SEND, sink.run_id)
        surface = json.dumps(args, ensure_ascii=False, default=str)
        intent = RuntimeEffectIntent(
            effect_id=effect_id, effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
            session_id=sink.session_id, run_id=sink.run_id, phase=sink.phase,
            provider_id=sink.provider_id, turn=sink.send_index, display_ref=name,
            args_digest=compute_args_digest(surface), replay_class=ReplayClass.UNSAFE,
            delivery_batch_id=sink.delivery_batch_id,
        )
        sink.mutations.begin_provider_effect(
            sink.session_id, sink.run_id, intent,
            delivery_batch_id=sink.delivery_batch_id,
        )
        try:
            reply = getattr(self.provider, name)(*args, **kwargs)
        except Exception as exc:
            try:
                from codey.providers import error_classification as errors

                overflow_or_prep = isinstance(
                    exc, (errors.ContextOverflowError, errors.RequestPrepError)
                )
            except Exception:
                overflow_or_prep = False
            # Overflow deterministically produced no usable reply and prep
            # failures sent nothing: settle NOT_SENT so the same batch can
            # safely retry/supersede. All other faults stay MAYBE_SENT.
            not_sent = (
                overflow_or_prep
                or isinstance(exc, StopIteration)
                or getattr(exc, "provider_failure_kind", "") == "not_submitted"
            )
            sent_state = SENT_STATE_NOT_SENT if not_sent else SENT_STATE_MAYBE_SENT
            settlement = RuntimeEffectSettlement(
                    effect_id=effect_id, effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
                    session_id=sink.session_id, run_id=sink.run_id,
                    status=SETTLEMENT_STATUS_ERROR, error_code=type(exc).__name__[:80],
                    sent_state=sent_state,
                )
            sink.mutations.settle_provider_effect(
                sink.session_id, sink.run_id,
                settlement,
            )
            raise
        settlement = RuntimeEffectSettlement(
                effect_id=effect_id, effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
                session_id=sink.session_id, run_id=sink.run_id,
                status=SETTLEMENT_STATUS_OK, sent_state=SENT_STATE_SETTLED,
            )
        messages = _reply_messages(reply) if sink.durable is not None else ()
        if sink.defer_generation_settlement:
            sink.defer_provider_settlement(settlement, messages)
        else:
            sink.mutations.settle_provider_effect(
                sink.session_id, sink.run_id, settlement, messages=messages,
            )
        sink.delivery_batch_id = ""
        return reply


def _reply_messages(reply: Any) -> tuple[tuple[str, dict[str, object]], ...]:
    if isinstance(reply, AssistantTurn) and reply.tool_calls:
        return (("tool_call", {"content": reply.text, "tool_calls": [
            {"id": call.id, "type": "function", "function": {
                "name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False),
            }} for call in reply.tool_calls
        ]}),)
    # Plain assistant text is committed by terminal task settlement. Keeping
    # it out of the provider effect settlement gives each logical answer one
    # durable owner and prevents a provider receipt plus completion commit
    # from producing duplicate history entries. Tool calls remain here because
    # they are context facts needed before the next provider turn.
    return ()


__all__ = ["KernelEffectSink", "KernelRecordedProvider", "admitted_durable"]
