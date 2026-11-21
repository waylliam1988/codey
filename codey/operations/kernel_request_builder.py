"""Typed assembly for the shared kernel request boundary.

The four production entries keep their own authorization, recovery and
resource decisions. This builder only owns the repeated transport assembly;
execution and observation dependencies remain explicit at each call site.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from codey.memory.contracts import MemoryTrace
from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
)
from codey.operations.provider_session import adapt_provider
from codey.providers.attempt_gate import AttemptGateCallback
from codey.utils.refs import digest_text
from codey.workspace.context_admission import ContextCandidate
from codey.workspace.context_selection import ContextSpan, replace_context_text, select_context_sources

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
    memory_sources: tuple[ContextCandidate, ...] = ()
    memory_spans: tuple[ContextSpan, ...] = ()
    memory_traces: tuple[MemoryTrace, ...] = ()
    collect_memory_candidates: bool = False
    admit_hints: bool = True
    attempt_gate: AttemptGateCallback | None = None
    attempt_admit_hints: Callable[[], bool] | None = None
    stop_flag: StopSignal | None = None
    stagnant_turns: int | None = None
    window_ref: ProtocolWindowRef | None = None
    attempt_validate_memory_sources: Callable[[tuple[ContextCandidate, ...]], bool] | None = None

    def build(
        self,
        *,
        execution: KernelExecutionDeps = _DEFAULT_EXECUTION,
        observation: KernelObservationDeps = _DEFAULT_OBSERVATION,
    ) -> KernelRunRequest:
        registered = {span.key for span in self.memory_spans}
        selected = (select_context_sources(self.memory_sources)
                    if all(source.key in registered for source in self.memory_sources) else self.memory_sources)

        stale = (bool(selected) and self.attempt_validate_memory_sources is not None
                 and not self.attempt_validate_memory_sources(self.memory_sources))
        if stale:
            selected = ()
        selected_keys = {source.key for source in selected}
        traces = tuple(replace(trace, reason="source_snapshot_changed" if stale else "combined_over_budget")
                       if trace.reason == "selected" and "memory_" + digest_text(trace.item_id) not in selected_keys
                       else trace for trace in self.memory_traces)

        def validate_selected(actual: tuple[ContextCandidate, ...]) -> bool:
            return (actual == selected and (not actual or self.attempt_validate_memory_sources is None
                    or self.attempt_validate_memory_sources(self.memory_sources)))

        return KernelRunRequest(
            task_guidance=self.task_guidance,
            transport=KernelTransportDeps(
                provider=adapt_provider(self.provider),
                run_id=self.run_id,
                effect_scope=self.effect_scope,
                provider_id=self.provider_id,
                user_task=self.user_task,
                context_text=replace_context_text(self.context_text, self.memory_sources, selected, spans=self.memory_spans),
                context_scope=self.context_scope,
                memory_sources=selected,
                memory_traces=traces,
                collect_memory_candidates=self.collect_memory_candidates,
                admit_hints=self.admit_hints,
                attempt_gate=self.attempt_gate,
                attempt_admit_hints=self.attempt_admit_hints,
                attempt_validate_memory_sources=validate_selected,
                stop_flag=self.stop_flag,
                stagnant_turns=self.stagnant_turns,
                window_ref=self.window_ref,
            ),
            execution=execution,
            observation=observation,
        )



__all__ = ["KernelRunRequestBuilder"]
