"""Typed assembly for the shared kernel request boundary.

The four production entries keep their own authorization, recovery and
resource decisions. This builder only owns the repeated transport assembly;
execution and observation dependencies remain explicit at each call site.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
)
from codey.operations.provider_session import adapt_provider
from codey.operations.represented_history import represented_history_refs, represented_inputs
from codey.providers.attempt_gate import AttemptGateCallback
from codey.workspace.context_admission import GhostContextSource
from codey.workspace.ghost_selection import GhostContextSpan, replace_ghost_text, select_ghost_sources

if TYPE_CHECKING:
    from codey.operations.kernel_contracts import StopSignal
    from codey.runtime.core.protocol_window import ProtocolWindowRef

_DEFAULT_EXECUTION = KernelExecutionDeps()
_DEFAULT_OBSERVATION = KernelObservationDeps()


@dataclass(frozen=True)
class KernelRunRequestBuilder:
    provider: object
    run_id: str
    effect_scope: str
    provider_id: str
    user_task: str
    task_guidance: str = ""
    context_text: str = ""
    context_scope: str = "request"
    ghost_sources: tuple[GhostContextSource, ...] = ()
    ghost_spans: tuple[GhostContextSpan, ...] = ()
    admit_hints: bool = True
    attempt_gate: AttemptGateCallback | None = None
    attempt_admit_hints: Callable[[], bool] | None = None
    stop_flag: StopSignal | None = None
    stagnant_turns: int | None = None
    window_ref: ProtocolWindowRef | None = None
    attempt_validate_ghost_sources: Callable[[tuple[GhostContextSource, ...]], bool] | None = None

    def build(
        self,
        *,
        execution: KernelExecutionDeps = _DEFAULT_EXECUTION,
        observation: KernelObservationDeps = _DEFAULT_OBSERVATION,
    ) -> KernelRunRequest:
        from codey.operations.personal_context import supplement_personal_context

        personal = supplement_personal_context(
            text=self.context_text, sources=self.ghost_sources, scope=self.context_scope,
            run_id=self.run_id, task=self.user_task, enabled=self.admit_hints,
            execution=execution, validate=self.attempt_validate_ghost_sources,
            spans=self.ghost_spans,
        )
        def input_entry_id(run_id: str) -> str:
            sink = execution.intent_sink
            if sink is None:
                return ""
            try:
                value = sink.durable.task(run_id).state.get("input_entry_id")
                return value if isinstance(value, str) else ""
            except KeyError:
                return ""

        inputs = represented_inputs(self.provider, personal.sources, input_entry_id)
        represented = represented_history_refs(self.provider) | frozenset(ref for ref, _text in inputs)
        # Legacy callers without a registered layout retain their original
        # projection. Never guess ownership from a matching substring.
        registered = {span.key for span in personal.spans}
        selected = (select_ghost_sources(personal.sources, represented=represented)
                    if all(source.key in registered for source in personal.sources) else personal.sources)
        removed_refs = represented.intersection(frozenset(
            ref for source in personal.sources for ref in _source_refs(source)
        )) - frozenset(ref for source in selected for ref in _source_refs(source))

        def validate_selected(actual: tuple[GhostContextSource, ...]) -> bool:
            return (actual == selected
                    and (not personal.sources or personal.validate is None or personal.validate(personal.sources))
                    and removed_refs <= (represented_history_refs(self.provider) | frozenset(
                        ref for ref, _text in represented_inputs(self.provider, personal.sources, input_entry_id))))

        return KernelRunRequest(
            task_guidance=self.task_guidance,
            transport=KernelTransportDeps(
                provider=adapt_provider(self.provider),
                run_id=self.run_id,
                effect_scope=self.effect_scope,
                provider_id=self.provider_id,
                user_task=self.user_task,
                context_text=replace_ghost_text(personal.text, personal.sources, selected, spans=personal.spans),
                context_scope=self.context_scope,
                ghost_sources=selected,
                required_history_refs=removed_refs,
                required_history_inputs=inputs,
                admit_hints=self.admit_hints,
                attempt_gate=self.attempt_gate,
                attempt_admit_hints=self.attempt_admit_hints,
                attempt_validate_ghost_sources=validate_selected,
                stop_flag=self.stop_flag,
                stagnant_turns=self.stagnant_turns,
                window_ref=self.window_ref,
            ),
            execution=execution,
            observation=observation,
        )


def _source_refs(source: GhostContextSource) -> frozenset[str]:
    from codey.ghost.observation_index import history_refs

    return history_refs(source.text)


__all__ = ["KernelRunRequestBuilder"]
