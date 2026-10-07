"""Durable effect receipts for the shared task turn kernel."""

from __future__ import annotations

import json
from typing import Any

from codey.operations.provider_session import ProviderAdapter
from codey.runtime.core.models import ToolCall
from codey.runtime.effects.effect_records import (
    EFFECT_CATEGORY_PROVIDER_SEND,
    EFFECT_CATEGORY_TOOL_CALL,
    SENT_STATE_MAYBE_SENT,
    SENT_STATE_NOT_SENT,
    SENT_STATE_SETTLED,
    SETTLEMENT_STATUS_ERROR,
    SETTLEMENT_STATUS_OK,
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


class KernelEffectSink:
    """Commit one turn's tool intents before any tool can run."""

    def __init__(self, mutations: Any, *, session_id: str, run_id: str,
                 provider_id: str, phase: str = "writer",
                 recovered_batch_id: str = "",
                 managed_outputs: Any = None) -> None:
        self.mutations = mutations
        self.session_id = session_id
        self.run_id = run_id
        self.provider_id = provider_id
        self.phase = phase
        self.delivery_batch_id = recovered_batch_id
        self.send_index = 0
        self.managed_outputs = managed_outputs
        store = RuntimeEffectStore(mutations.session_log)
        previous = store.load_effects(session_id, run_id)
        self._replay_classes = {row.intent.effect_id: row.intent.replay_class for row in previous}
        self._old_pending = {row.intent.effect_id for row in previous if row.is_pending}

    def begin_turn(self, items: list[tuple[str, ToolCall, int]], *, turn: int, specs: Any = None) -> None:
        intents: list[RuntimeEffectIntent] = []
        batch_items: list[DeliveryBatchItem] = []
        for identity, call, index in items:
            if identity in self._old_pending:
                continue
            name = _runtime_name(str(call.name or ""))
            spec = specs.get(call.name) if specs is not None else spec_for_tool(str(call.name or ""))
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
                replay_args=replay_args,
            ))
            batch_items.append(DeliveryBatchItem(
                tool_index=index, tool_name=name, ref=identity,
                replay_class=replay_class,
            ))
        if not batch_items:
            return
        batch_tuple = tuple(batch_items)
        batch_id = new_batch_id(self.run_id, turn)
        self.mutations.begin_tool_batch(
            self.session_id, self.run_id, intents=tuple(intents),
            delivery_intent=DeliveryBatchIntent(
                batch_id=batch_id, session_id=self.session_id, run_id=self.run_id,
                turn=turn, items=batch_tuple,
                batch_digest=compute_batch_digest(batch_tuple),
            ),
        )
        self.delivery_batch_id = batch_id

    def has_unsettled(self, identity: str) -> bool:
        return identity in self._old_pending

    def settle(self, identity: str, ok: bool, *, result: Any = None, exit_code: int | None = None) -> None:
        from codey.operations.kernel_receipts import result_receipt_fields

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
        self.mutations.settle_tool_effect(
            self.session_id, self.run_id,
            RuntimeEffectSettlement(
                effect_id=identity, effect_category=EFFECT_CATEGORY_TOOL_CALL,
                session_id=self.session_id, run_id=self.run_id,
                status=SETTLEMENT_STATUS_OK if ok else SETTLEMENT_STATUS_ERROR,
                error_code="" if ok else "tool_error",
                replay_class=self._replay_classes.get(identity, ReplayClass.UNSAFE),
                **fields,
                exit_code=resolved_exit,
            ),
        )

class KernelRecordedProvider(ProviderAdapter):
    """Record provider sends and link result delivery to the preceding tool batch."""

    def __init__(self, provider: Any, sink: KernelEffectSink) -> None:
        super().__init__(provider)
        self.sink = sink

    def _send(self, name: str, *args: Any, **kwargs: Any) -> Any:
        sink = self.sink
        sink.send_index += 1
        effect_id = new_effect_id(EFFECT_CATEGORY_PROVIDER_SEND, sink.run_id)
        surface = json.dumps(args, ensure_ascii=False, default=str)
        intent = RuntimeEffectIntent(
            effect_id=effect_id, effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
            session_id=sink.session_id, run_id=sink.run_id, phase=sink.phase,
            provider_id=sink.provider_id, turn=sink.send_index, display_ref=name,
            args_digest=compute_args_digest(surface), replay_class=ReplayClass.UNSAFE,
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
            not_sent = overflow_or_prep or getattr(exc, "provider_failure_kind", "") == "not_submitted"
            sent_state = SENT_STATE_NOT_SENT if not_sent else SENT_STATE_MAYBE_SENT
            sink.mutations.settle_provider_effect(
                sink.session_id, sink.run_id,
                RuntimeEffectSettlement(
                    effect_id=effect_id, effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
                    session_id=sink.session_id, run_id=sink.run_id,
                    status=SETTLEMENT_STATUS_ERROR, error_code=type(exc).__name__[:80],
                    sent_state=sent_state,
                ),
            )
            raise
        sink.mutations.settle_provider_effect(
            sink.session_id, sink.run_id,
            RuntimeEffectSettlement(
                effect_id=effect_id, effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
                session_id=sink.session_id, run_id=sink.run_id,
                status=SETTLEMENT_STATUS_OK, sent_state=SENT_STATE_SETTLED,
            ),
        )
        sink.delivery_batch_id = ""
        return reply


__all__ = ["KernelEffectSink", "KernelRecordedProvider"]
