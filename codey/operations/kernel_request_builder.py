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
from codey.providers.attempt_gate import AttemptGateCallback
from codey.workspace.context_admission import GhostContextSource

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
    admit_hints: bool = True
    attempt_gate: AttemptGateCallback | None = None
    attempt_admit_hints: Callable[[], bool] | None = None
    stop_flag: StopSignal | None = None
    stagnant_turns: int | None = None
    window_ref: ProtocolWindowRef | None = None

    def build(
        self,
        *,
        execution: KernelExecutionDeps = _DEFAULT_EXECUTION,
        observation: KernelObservationDeps = _DEFAULT_OBSERVATION,
    ) -> KernelRunRequest:
        return KernelRunRequest(
            task_guidance=self.task_guidance,
            transport=KernelTransportDeps(
                provider=adapt_provider(self.provider),
                run_id=self.run_id,
                effect_scope=self.effect_scope,
                provider_id=self.provider_id,
                user_task=self.user_task,
                context_text=self.context_text,
                context_scope=self.context_scope,
                ghost_sources=self.ghost_sources,
                admit_hints=self.admit_hints,
                attempt_gate=self.attempt_gate,
                attempt_admit_hints=self.attempt_admit_hints,
                stop_flag=self.stop_flag,
                stagnant_turns=self.stagnant_turns,
                window_ref=self.window_ref,
            ),
            execution=execution,
            observation=observation,
        )


__all__ = ["KernelRunRequestBuilder"]
