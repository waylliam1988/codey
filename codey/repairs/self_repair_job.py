"""Single owner for the self-repair job model (leaf).

Both the repair supervisor and the isolated worker consume this model.
The leaf depends only on failure-fact sanitation, never on the supervisor
or the worker, so the previous supervisor <-> worker import cycle is gone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codey.providers.diagnostics import sanitize_failure_facts


@dataclass(frozen=True)
class SelfRepairJob:
    provider_id: str
    failure_kind: str
    failure_stage: str = ""
    enqueued_at: float = 0.0
    next_retry_at: float = 0.0
    failure_facts: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "failure_facts", sanitize_failure_facts(self.failure_facts))


__all__ = ["SelfRepairJob"]
